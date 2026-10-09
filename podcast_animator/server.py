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

import numpy as np
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import media, transcribe
from .paths import DATA_DIR, WEB_DIR, ensure_dirs
from .pipeline import EXTRA_SPK_BASE, Worker, clip_tracks, clip_words, default_settings, new_clip
from .render.characters import DEFAULT_STYLE, get_character, list_characters, save_character
from .render.scene import THEMES
from .render.video import RenderOptions, character_preview
from .store import ProjectStore, ai_config, load_settings, save_settings
from .transcript import retime_text, sentences, sentences_by_speaker

log = logging.getLogger(__name__)

UPLOAD_DIR = DATA_DIR / "uploads"
MEDIA_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
             ".opus", ".wma"}


def ranged_response(data: bytes, media_type: str, range_header: str | None) -> Response:
    """Antwort mit HTTP-Range-Unterstützung – ohne kann der Browser im Audio nicht springen."""
    headers = {"Accept-Ranges": "bytes"}
    size = len(data)
    if range_header and range_header.startswith("bytes="):
        first = range_header[6:].split(",")[0].strip()
        a_txt, _, b_txt = first.partition("-")
        try:
            if a_txt:
                a, b = int(a_txt), int(b_txt) if b_txt else size - 1
            else:  # "bytes=-500" = die letzten 500 Bytes
                a, b = max(0, size - int(b_txt)), size - 1
        except ValueError:
            a, b = 0, size - 1
        if a >= size or a > b:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        b = min(b, size - 1)
        headers["Content-Range"] = f"bytes {a}-{b}/{size}"
        return Response(data[a:b + 1], status_code=206, media_type=media_type, headers=headers)
    return Response(data, media_type=media_type, headers=headers)


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
        # Versionsstempel an CSS/JS hängen, damit der Browser nach einem Update nicht die alte Oberfläche zeigt
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        for name in ("app.js", "style.css"):
            stamp = int((WEB_DIR / name).stat().st_mtime)
            html = html.replace(f"/static/{name}", f"/static/{name}?v={stamp}")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

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
            "version": app_version(),
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
    def audio(pid: str, start: float, end: float, request: Request):
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
        return ranged_response(buf.getvalue(), "audio/wav", request.headers.get("range"))

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
        if "extra_speakers" in body:
            known_chars = {c["id"] for c in list_characters()}
            extras, used = [], set()
            for x in body["extra_speakers"] or []:
                spk, char = int(x.get("spk", 0)), str(x.get("char", ""))
                if spk < EXTRA_SPK_BASE or char not in known_chars or spk in used:
                    raise HTTPException(400, f"Ungültige Person: {x}")
                used.add(spk)
                extras.append({"spk": spk, "char": char})
            upd["extra_speakers"] = extras
        allowed = {int(sp["spk"]) for sp in p.get("speakers", [])}
        allowed |= {x["spk"] for x in upd.get("extra_speakers", clip.get("extra_speakers") or [])}
        if "regions" in body:
            upd["regions"] = _clean_regions(body["regions"] or [], allowed, start, end)
        elif "extra_speakers" in body and clip.get("regions"):
            upd["regions"] = [r for r in clip["regions"] if r["spk"] in allowed]
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
        words = upd.get("words", clip.get("words"))
        if words and ("lines" in body or "extra_speakers" in body):
            # Wörter von entfernten Personen (oder "wie erkannt") bekommen wieder den erkannten Sprecher
            tr = store.transcript(pid) or {"words": []}
            upd["words"] = _restore_unknown_speakers(words, tr["words"], allowed)
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
        lines = [dict(g.to_dict(), orig_spk=g.spk) for g in sentences_by_speaker(words)]
        return {"lines": lines, "edited": bool(clip.get("words")), "tracks": clip_tracks(p, clip),
                "regions": clip.get("regions") or [], "extra_speakers": clip.get("extra_speakers") or []}

    @app.get("/api/projects/{pid}/waveform")
    def waveform(pid: str, start: float, end: float, bins: int = 800):
        """Spitzenwerte der Tonspur für die Zeitleiste (0..1)."""
        project_or_404(pid)
        end = min(end, start + 600)
        data, _ = media.read_wav(store.dir(pid) / "audio16k.wav", start, end)
        bins = max(10, min(4000, bins))
        if len(data) == 0:
            return {"peaks": []}
        edges = np.linspace(0, len(data), bins + 1).astype(int)
        peaks = [float(np.abs(data[a:b]).max()) if b > a else 0.0 for a, b in zip(edges, edges[1:])]
        top = max(peaks) or 1.0
        return {"peaks": [round(v / top, 3) for v in peaks]}

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


def app_version() -> str:
    """Kurzer Git-Commit (falls per git geladen), sonst die Paketversion."""
    from .paths import APP_DIR

    try:
        head = (APP_DIR / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1]
            f = APP_DIR / ".git" / ref
            if f.exists():
                return f.read_text().strip()[:7]
            for line in (APP_DIR / ".git" / "packed-refs").read_text().splitlines():
                if line.endswith(" " + ref):
                    return line[:7]
        return head[:7]
    except Exception:
        from importlib.metadata import version

        try:
            return version("podcast-animator")
        except Exception:
            return "?"


def _hint(key: str | None) -> str:
    return ("…" + key[-4:]) if key else ""


def _clean_regions(regions: list[dict], allowed: set[int], start: float, end: float) -> list[dict]:
    """Sprechbereiche prüfen, auf den Clip beschneiden und Überlappungen je Person zusammenfassen."""
    by_spk: dict[int, list[list[float]]] = {}
    for r in regions:
        try:
            spk, s, e = int(r["spk"]), float(r["s"]), float(r["e"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, f"Ungültiger Bereich: {r}") from None
        if spk not in allowed:
            raise HTTPException(400, f"Unbekannte Person in Bereich: {spk}")
        s, e = max(s, start), min(e, end)
        if e - s >= 0.1:
            by_spk.setdefault(spk, []).append([s, e])
    out = []
    for spk, items in by_spk.items():
        items.sort()
        merged: list[list[float]] = []
        for s, e in items:
            if merged and s <= merged[-1][1] + 0.05:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        out += [{"spk": spk, "s": round(s, 3), "e": round(e, 3)} for s, e in merged]
    return sorted(out, key=lambda r: r["s"])


def _restore_unknown_speakers(words: list[dict], source: list[dict], allowed: set[int]) -> list[dict]:
    """Wörter mit unbekanntem Sprecher -> Sprecher aus dem Transkript (größte Überlappung).

    Im Editor dazugeschriebener Text ("added") einer entfernten Person fällt weg."""
    out = []
    for w in words:
        if w.get("spk") in allowed:
            out.append(w)
            continue
        if w.get("added"):
            continue
        best, best_ov = None, 0.0
        for o in source:
            if o["e"] <= w["s"] or o["s"] >= w["e"]:
                continue
            ov = min(o["e"], w["e"]) - max(o["s"], w["s"])
            if ov > best_ov and o.get("spk") in allowed:
                best, best_ov = o["spk"], ov
        if best is not None:
            out.append(dict(w, spk=best))
    return out


def _words_from_lines(words: list[dict], lines: list[dict]) -> list[dict]:
    """Baut aus bearbeiteten Zeilen (Text/Sprecher) wieder Wörter mit Zeitstempeln.

    Zeilen mit "new": true sind im Editor neu hinzugefügt (z.B. Text für jemanden, der gleichzeitig redet)."""
    out: list[dict] = []
    for line in lines:
        s, e = float(line["s"]), float(line["e"])
        if line.get("new"):
            if e - s >= 0.1 and str(line.get("text", "")).strip():
                out.extend(dict(w, added=True) for w in retime_text([{"w": "", "s": s, "e": e, "spk": int(line["spk"])}],
                                                                    str(line["text"]).strip(), int(line["spk"])))
            continue
        orig = line.get("orig_spk")
        old = [w for w in words if s - 0.01 <= w["s"] <= e + 0.01 and (orig is None or w.get("spk") == orig)]
        if not old:
            continue
        text = str(line.get("text", "")).strip()
        if not text:  # Zeile gelöscht -> keine Untertitel, Figur bewegt den Mund nicht
            continue
        added = all(w.get("added") for w in old)  # war schon dazugeschriebener Text
        out.extend(dict(w, added=True) if added else w
                   for w in retime_text(old, text, int(line.get("spk", old[0].get("spk", 0)))))
    return sorted(out, key=lambda w: w["s"])
