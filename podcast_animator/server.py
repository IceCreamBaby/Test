"""Lokale Web-Oberfläche (läuft nur auf deinem PC, http://127.0.0.1:7860)."""
from __future__ import annotations

import io
import logging
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import media, transcribe
from .paths import DATA_DIR, WEB_DIR, ensure_dirs
from .pipeline import Worker, clip_words, default_settings, new_clip
from .render.characters import DEFAULT_STYLE, get_character, list_characters, save_character
from .render.scene import THEMES
from .render.video import RenderOptions, character_preview
from .store import ProjectStore, ai_config, load_settings, save_settings
from .transcript import retime_text, sentences

log = logging.getLogger(__name__)

UPLOAD_DIR = DATA_DIR / "uploads"
MEDIA_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
             ".opus", ".wma"}


def create_app() -> FastAPI:
    ensure_dirs()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    store = ProjectStore()
    worker = Worker(store)
    app = FastAPI(title="Podcast Animator")
    app.state.store = store
    app.state.worker = worker

    def project_or_404(pid: str) -> dict:
        p = store.get(pid)
        if p is None:
            raise HTTPException(404, "Projekt nicht gefunden")
        return p

    def clip_or_404(p: dict, cid: str) -> dict:
        c = next((c for c in p["clips"] if c["id"] == cid), None)
        if c is None:
            raise HTTPException(404, "Clip nicht gefunden")
        return c

    # ------------------------------------------------------------------ Seiten
    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    # ------------------------------------------------------------------ Status
    @app.get("/api/state")
    def state():
        s = load_settings()
        ai = ai_config(s)
        return {
            "projects": store.list(),
            "worker": worker.status(),
            "characters": [{k: v for k, v in c.items() if not k.startswith("_")} | {"builtin": c.get("_builtin")}
                           for c in sorted(list_characters(), key=lambda c: (
                               s["default_chars"].index(c["id"]) if c["id"] in s["default_chars"] else 99,
                               c.get("name", "")))],
            "themes": list(THEMES),
            "models": transcribe.MODEL_CHOICES,
            "cuda": transcribe.cuda_available(),
            "settings": {"watermark": s["watermark"], "sign_text": s["sign_text"],
                         "podcast_context": s["podcast_context"], "default_chars": s["default_chars"],
                         "hotwords": s.get("hotwords", ""),
                         "ai_provider": s.get("ai_provider", "gemini"), "gemini_model": s.get("gemini_model", ""),
                         "has_ai": ai["available"], "ai_name": ai["name"], "ai_active": ai["provider"],
                         "claude_key_hint": _hint(s.get("anthropic_api_key")),
                         "gemini_key_hint": _hint(s.get("gemini_api_key"))},
            "defaults": default_settings(),
            "data_dir": str(DATA_DIR),
        }

    @app.post("/api/settings")
    def settings(body: dict = Body(...)):
        save_settings(body)
        return {"ok": True}

    # ------------------------------------------------------------------ Upload & Projekte
    @app.put("/api/upload")
    async def upload(request: Request, filename: str):
        ext = Path(filename).suffix.lower()
        if ext not in MEDIA_EXT:
            raise HTTPException(400, f"Dateityp {ext or '?'} wird nicht unterstützt.")
        token = secrets.token_hex(8)
        dst = UPLOAD_DIR / f"{token}{ext}"
        with open(dst, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        return {"upload_id": token + ext, "size": dst.stat().st_size}

    @app.post("/api/projects")
    def create_project(body: dict = Body(...)):
        settings_in = body.get("settings") or {}
        settings = default_settings()
        render = dict(settings["render"])
        render.update(settings_in.pop("render", {}) or {})
        settings.update({k: v for k, v in settings_in.items() if k in settings})
        settings["render"] = RenderOptions.from_dict(render).to_dict()
        rng = body.get("range") or [None, None]
        try:
            start, end = media.parse_ts(rng[0]), media.parse_ts(rng[1])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if start is not None or end is not None:
            if start is not None and end is not None and end <= start:
                raise HTTPException(400, "Das Ende muss nach dem Start liegen.")
            settings["range"] = [start, end]
        if body.get("upload_id"):
            src = UPLOAD_DIR / Path(body["upload_id"]).name
            if not src.exists():
                raise HTTPException(400, "Upload nicht gefunden.")
            name = body.get("name") or Path(body.get("filename") or src.name).stem
        elif body.get("path"):
            src = Path(str(body["path"]).strip().strip('"'))
            if not src.is_file():
                raise HTTPException(400, f"Datei nicht gefunden: {src}")
            name = body.get("name") or src.stem
        else:
            raise HTTPException(400, "Bitte eine Datei hochladen oder einen Pfad angeben.")
        p = store.create(name, settings)
        if body.get("upload_id"):
            dst = store.dir(p["id"]) / ("source" + src.suffix.lower())
            shutil.move(str(src), dst)
            source = str(dst)
        else:
            source = str(src.resolve())
        store.update(p["id"], lambda q: q.update(source=source, source_name=body.get("filename") or src.name))
        worker.submit("process", p["id"])
        return store.get(p["id"])

    @app.get("/api/projects/{pid}")
    def get_project(pid: str):
        p = project_or_404(pid)
        p["worker"] = worker.status()
        return p

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        project_or_404(pid)
        worker.cancel_project(pid)
        store.delete(pid)
        return {"ok": True}

    @app.post("/api/projects/{pid}/rename")
    def rename(pid: str, body: dict = Body(...)):
        project_or_404(pid)
        return store.update(pid, lambda q: q.update(name=str(body.get("name") or q["name"])[:120]))

    @app.post("/api/projects/{pid}/reprocess")
    def reprocess(pid: str, body: dict = Body(default={})):
        project_or_404(pid)

        def fn(q):
            for k in ("num_speakers", "model", "mode", "clip_count", "min_len", "max_len", "use_ai"):
                if k in body:
                    q["settings"][k] = body[k]
            q["status"] = "new"
            q["error"] = None
        store.update(pid, fn)
        worker.submit("process", pid)
        return {"ok": True}

    @app.post("/api/projects/{pid}/cancel")
    def cancel(pid: str):
        worker.cancel_project(pid)
        return {"ok": True}

    @app.post("/api/projects/{pid}/settings")
    def project_settings(pid: str, body: dict = Body(...)):
        """Standard-Rendereinstellungen eines Projekts (gelten für alle Clips ohne eigene Einstellung)."""
        project_or_404(pid)

        def fn(q):
            r = dict(q["settings"].get("render") or {})
            r.update(body.get("render") or {})
            q["settings"]["render"] = RenderOptions.from_dict(r).to_dict()
            for c in q["clips"]:
                if c.get("status") == "done":
                    c["stale"] = True
        return store.update(pid, fn)

    @app.post("/api/projects/{pid}/open")
    def open_folder(pid: str):
        project_or_404(pid)
        folder = store.dir(pid) / "clips"
        folder.mkdir(exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            raise HTTPException(500, f"Ordner konnte nicht geöffnet werden: {exc}")
        return {"ok": True, "path": str(folder)}

    # ------------------------------------------------------------------ Sprecher
    @app.post("/api/projects/{pid}/speakers")
    def set_speakers(pid: str, body: dict = Body(...)):
        p = project_or_404(pid)
        chars = {c["id"] for c in list_characters()}
        known = {sp["spk"]: sp for sp in p["speakers"]}
        new = []
        for item in body.get("speakers", []):
            spk = int(item["spk"])
            if spk not in known:
                continue
            cid = item.get("char") or "none"
            if cid != "none" and cid not in chars:
                raise HTTPException(400, f"Unbekannte Figur: {cid}")
            new.append({**known[spk], "char": cid})
        for spk, sp in known.items():  # nicht übergebene Sprecher behalten
            if spk not in {n["spk"] for n in new}:
                new.append(sp)

        def fn(q):
            q["speakers"] = new
            for c in q["clips"]:
                if c.get("status") == "done":
                    c["stale"] = True
        store.update(pid, fn)
        remembered = worker.pipeline.remember_voices(pid) if body.get("remember", True) else 0
        return {"ok": True, "remembered": remembered}

    @app.post("/api/projects/{pid}/rediarize")
    def rediarize(pid: str, body: dict = Body(default={})):
        project_or_404(pid)
        n = body.get("num_speakers")
        worker.submit("rediarize", pid, num_speakers=int(n) if n is not None else None,
                      refind=bool(body.get("refind", False)))
        return {"ok": True}

    @app.get("/api/projects/{pid}/audio")
    def audio(pid: str, start: float, end: float):
        project_or_404(pid)
        end = min(end, start + 240)
        data, sr = media.read_wav(store.dir(pid) / "audio16k.wav", start, end)
        buf = io.BytesIO()
        import wave
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes((data.clip(-1, 1) * 32767).astype("<i2").tobytes())
        return Response(buf.getvalue(), media_type="audio/wav")

    @app.get("/api/projects/{pid}/file")
    def file(pid: str, path: str, download: int = 0):
        project_or_404(pid)
        base = store.dir(pid).resolve()
        f = (base / path).resolve()
        if base not in f.parents or not f.is_file():
            raise HTTPException(404, "Datei nicht gefunden")
        return FileResponse(f, filename=f.name if download else None)

    @app.get("/api/projects/{pid}/transcript")
    def transcript(pid: str, start: float = 0.0, end: float = 1e9):
        project_or_404(pid)
        tr = store.transcript(pid) or {"words": []}
        lines = sentences([w for w in tr["words"] if start <= w["s"] < end])
        return {"lines": [g.to_dict() for g in lines[:3000]]}

    # ------------------------------------------------------------------ Clips
    @app.post("/api/projects/{pid}/find_clips")
    def find_clips(pid: str, body: dict = Body(default={})):
        project_or_404(pid)
        worker.submit("find_clips", pid, count=body.get("count"), use_ai=body.get("use_ai", body.get("use_claude")),
                      min_len=body.get("min_len"), max_len=body.get("max_len"), append=body.get("append", True))
        return {"ok": True}

    @app.post("/api/projects/{pid}/clips")
    def add_clip(pid: str, body: dict = Body(...)):
        p = project_or_404(pid)
        try:
            start, end = media.parse_ts(body.get("start")), media.parse_ts(body.get("end"))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if start is None or end is None or end - start < 2:
            raise HTTPException(400, "Bitte gültigen Start und Ende angeben (mindestens 2 Sekunden).")
        end = min(end, p.get("duration") or end)
        clip = new_clip(start, end, body.get("title", ""), "manuell hinzugefügt")
        store.update(pid, lambda q: q["clips"].append(clip))
        if body.get("render"):
            worker.submit("render", pid, cid=clip["id"])
        return clip

    @app.patch("/api/projects/{pid}/clips/{cid}")
    def patch_clip(pid: str, cid: str, body: dict = Body(...)):
        p = project_or_404(pid)
        clip = clip_or_404(p, cid)
        upd: dict = {}
        if "title" in body:
            upd["title"] = str(body["title"])[:120]
        for k in ("start", "end"):
            if k in body:
                try:
                    upd[k] = round(float(media.parse_ts(body[k])), 2)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"Ungültige Zeit: {body[k]}")
        start, end = upd.get("start", clip["start"]), upd.get("end", clip["end"])
        if end - start < 1:
            raise HTTPException(400, "Clip ist zu kurz.")
        if "render" in body:
            upd["render"] = {k: v for k, v in (body["render"] or {}).items()
                             if k in RenderOptions.__dataclass_fields__}
        if "lines" in body:
            upd["words"] = _words_from_lines(clip_words(store, p, clip), body["lines"])
        elif clip.get("words") and ("start" in upd or "end" in upd):
            # bearbeitete Wörter an neuen Bereich anpassen, fehlende aus dem Transkript ergänzen
            tr = store.transcript(pid) or {"words": []}
            edited = [w for w in clip["words"] if start <= w["s"] < end]
            lo = min((w["s"] for w in edited), default=end)
            hi = max((w["e"] for w in edited), default=start)
            extra = [dict(w) for w in tr["words"] if start <= w["s"] < end and not (lo <= w["s"] < hi)]
            upd["words"] = sorted(edited + extra, key=lambda w: w["s"])
        if body.get("reset_words"):
            upd["words"] = None
        if clip.get("status") == "done":
            upd["stale"] = True
        store.update_clip(pid, cid, **upd)
        if body.get("render_now"):
            worker.submit("render", pid, cid=cid)
        return store.clip(pid, cid)

    @app.delete("/api/projects/{pid}/clips/{cid}")
    def delete_clip(pid: str, cid: str):
        p = project_or_404(pid)
        clip = clip_or_404(p, cid)
        for key in ("video", "srt", "thumb"):
            if clip.get(key):
                (store.dir(pid) / clip[key]).unlink(missing_ok=True)
        store.update(pid, lambda q: q.update(clips=[c for c in q["clips"] if c["id"] != cid]))
        return {"ok": True}

    @app.get("/api/projects/{pid}/clips/{cid}/lines")
    def clip_lines(pid: str, cid: str):
        p = project_or_404(pid)
        clip = clip_or_404(p, cid)
        words = clip_words(store, p, clip)
        return {"lines": [g.to_dict() for g in sentences(words)], "edited": bool(clip.get("words"))}

    @app.post("/api/projects/{pid}/clips/{cid}/render")
    def render(pid: str, cid: str):
        p = project_or_404(pid)
        clip_or_404(p, cid)
        worker.submit("render", pid, cid=cid)
        return {"ok": True}

    @app.post("/api/projects/{pid}/render_all")
    def render_all(pid: str, body: dict = Body(default={})):
        p = project_or_404(pid)
        only_missing = body.get("only_missing", True)
        n = 0
        for c in p["clips"]:
            if c.get("status") in ("queued", "rendering"):
                continue
            if only_missing and c.get("status") == "done" and not c.get("stale"):
                continue
            worker.submit("render", pid, cid=c["id"])
            n += 1
        return {"ok": True, "queued": n}

    @app.get("/api/projects/{pid}/clips/{cid}/preview.jpg")
    def preview(pid: str, cid: str, t: float | None = None):
        p = project_or_404(pid)
        clip_or_404(p, cid)
        try:
            data = worker.pipeline.preview(pid, cid, t)
        except Exception as exc:
            raise HTTPException(400, f"Vorschau fehlgeschlagen: {exc}")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ Figuren
    @app.post("/api/characters/preview.jpg")
    def char_preview(body: dict = Body(...)):
        style = {**DEFAULT_STYLE, **get_character(body.get("id", "figur")), **(body.get("style") or {})}
        return Response(character_preview(style, theme=body.get("theme", "lila"),
                                          sign_text=load_settings().get("sign_text", "")),
                        media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/characters")
    def char_save(body: dict = Body(...)):
        if not body.get("id") or not body.get("name"):
            raise HTTPException(400, "Figur braucht eine ID und einen Namen.")
        return save_character({**DEFAULT_STYLE, **body})

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception):  # verständliche Fehlermeldung statt "500"
        log.exception("Fehler bei %s", request.url.path)
        return JSONResponse({"detail": str(exc)}, status_code=500)

    return app


def _hint(key: str | None) -> str:
    return ("…" + key[-4:]) if key else ""


def _words_from_lines(words: list[dict], lines: list[dict]) -> list[dict]:
    """Baut aus bearbeiteten Zeilen (Text/Sprecher) wieder Wörter mit Zeitstempeln."""
    out: list[dict] = []
    for line in lines:
        s, e = float(line["s"]), float(line["e"])
        old = [w for w in words if s - 0.01 <= w["s"] <= e + 0.01]
        if not old:
            continue
        text = str(line.get("text", "")).strip()
        if not text:  # Zeile gelöscht -> keine Untertitel, Figur bewegt den Mund nicht
            continue
        out.extend(retime_text(old, text, int(line.get("spk", old[0].get("spk", 0)))))
    return sorted(out, key=lambda w: w["s"])
