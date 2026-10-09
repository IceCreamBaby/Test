"""Projekte und Einstellungen auf der Festplatte (JSON), thread-sicher."""
from __future__ import annotations

import copy
import json
import os
import secrets
import shutil
import threading
import time
from pathlib import Path

from .paths import PROJECTS_DIR, SETTINGS_FILE, ensure_dirs

DEFAULT_SETTINGS = {
    "ai_provider": "gemini",          # gemini | claude
    "anthropic_api_key": "",
    "gemini_api_key": "",
    "gemini_model": "gemini-flash-latest",
    "watermark": "@xxforcegamingxx · Fan-Animation",
    "sign_text": "PODCAST",
    "default_chars": ["rezo", "julien", "gast"],
    "podcast_context": "Video-Podcast mit Rezo und Julien Bam",
    "hotwords": "Rezo Julien Bam",
}

_lock = threading.RLock()


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- Einstellungen

def load_settings() -> dict:
    ensure_dirs()
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return data


def save_settings(update: dict) -> dict:
    with _lock:
        data = load_settings()
        for k, v in update.items():
            if k in DEFAULT_SETTINGS:
                data[k] = v
        _write_json(SETTINGS_FILE, data)
        return data


def ai_config(settings: dict | None = None) -> dict:
    """Aktive KI für die Clip-Auswahl: {'provider', 'name', 'api_key', 'model', 'available'}.

    Ist für den gewählten Anbieter kein Key hinterlegt, aber für den anderen, wird der andere verwendet.
    """
    s = settings or load_settings()
    keys = {
        "claude": s.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or "",
        "gemini": s.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "",
    }
    provider = s.get("ai_provider") if s.get("ai_provider") in keys else "gemini"
    if not keys[provider]:
        provider = next((p for p in ("gemini", "claude") if keys[p]), provider)
    return {
        "provider": provider,
        "name": {"claude": "Claude", "gemini": "Gemini"}[provider],
        "api_key": keys[provider] or None,
        "model": (s.get("gemini_model") or None) if provider == "gemini" else None,
        "available": bool(keys[provider]),
    }


# --------------------------------------------------------------------------- Projekte

class ProjectStore:
    """Hält Projekte im Speicher und schreibt Änderungen nach data/projects/<id>/project.json."""

    def __init__(self) -> None:
        ensure_dirs()
        self._projects: dict[str, dict] = {}
        self._live: dict[str, dict] = {}       # flüchtiger Fortschritt (nicht gespeichert)
        self._words: dict[str, dict] = {}      # Cache für Transkripte
        for d in sorted(PROJECTS_DIR.iterdir()) if PROJECTS_DIR.exists() else []:
            pj = d / "project.json"
            if pj.exists():
                try:
                    p = json.loads(pj.read_text(encoding="utf-8"))
                except Exception:
                    continue
                # nach einem Absturz hängengebliebene Jobs zurücksetzen
                if p.get("status") == "processing":
                    p["status"] = "error"
                    p["error"] = "Verarbeitung wurde unterbrochen (Programm beendet). Bitte erneut starten."
                for c in p.get("clips", []):
                    if c.get("status") in ("queued", "rendering"):
                        c["status"] = "new"
                self._projects[p["id"]] = p

    def dir(self, pid: str) -> Path:
        return PROJECTS_DIR / pid

    def create(self, name: str, settings: dict) -> dict:
        pid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        d = self.dir(pid)
        d.mkdir(parents=True, exist_ok=True)
        p = {"id": pid, "name": name or pid, "created": time.time(), "status": "new", "error": None,
             "settings": settings, "source": None, "offset": 0.0, "duration": 0.0, "speakers": [], "clips": [],
             "notes": []}
        with _lock:
            self._projects[pid] = p
            self._save(pid)
        return copy.deepcopy(p)

    def _save(self, pid: str) -> None:
        p = self._projects.get(pid)
        if p is not None:
            self.dir(pid).mkdir(parents=True, exist_ok=True)
            _write_json(self.dir(pid) / "project.json", p)

    def list(self) -> list[dict]:
        with _lock:
            out = []
            for p in sorted(self._projects.values(), key=lambda p: -p.get("created", 0)):
                out.append({"id": p["id"], "name": p["name"], "status": p["status"], "created": p.get("created"),
                            "clips": len(p.get("clips", [])),
                            "done": sum(1 for c in p.get("clips", []) if c.get("status") == "done"),
                            "live": self._live.get(p["id"])})
            return out

    def get(self, pid: str) -> dict | None:
        with _lock:
            p = self._projects.get(pid)
            if p is None:
                return None
            out = copy.deepcopy(p)
            out["live"] = copy.deepcopy(self._live.get(pid))
            return out

    def update(self, pid: str, fn) -> dict:
        """fn(project) verändert das Projekt in place; danach wird gespeichert."""
        with _lock:
            p = self._projects[pid]
            fn(p)
            self._save(pid)
            return copy.deepcopy(p)

    def set_live(self, pid: str, live: dict | None) -> None:
        with _lock:
            if live is None:
                self._live.pop(pid, None)
            else:
                self._live[pid] = live

    def update_clip(self, pid: str, cid: str, **fields) -> None:
        def fn(p):
            for c in p["clips"]:
                if c["id"] == cid:
                    c.update(fields)
        self.update(pid, fn)

    def clip(self, pid: str, cid: str) -> dict | None:
        p = self.get(pid)
        if not p:
            return None
        return next((c for c in p["clips"] if c["id"] == cid), None)

    def delete(self, pid: str) -> None:
        with _lock:
            self._projects.pop(pid, None)
            self._live.pop(pid, None)
            self._words.pop(pid, None)
        shutil.rmtree(self.dir(pid), ignore_errors=True)

    # ---------------------------------------------------------------- Transkript
    def save_transcript(self, pid: str, data: dict) -> None:
        with _lock:
            self._words[pid] = data
            _write_json(self.dir(pid) / "transcript.json", data)

    def transcript(self, pid: str) -> dict | None:
        with _lock:
            if pid in self._words:
                return self._words[pid]
            f = self.dir(pid) / "transcript.json"
            if not f.exists():
                return None
            data = json.loads(f.read_text(encoding="utf-8"))
            self._words[pid] = data
            if len(self._words) > 4:
                self._words.pop(next(iter(self._words)))
            return data
