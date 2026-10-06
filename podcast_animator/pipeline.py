"""Verarbeitungsschritte und Hintergrund-Warteschlange."""
from __future__ import annotations

import json
import logging
import queue
import secrets
import threading
import time
import traceback
from pathlib import Path

import numpy as np

from . import diarize, highlights, media, transcribe
from .render.characters import get_character, list_characters
from .render.video import RenderOptions, render_clip, render_preview, seats_from_mapping
from .store import ProjectStore, api_key, load_settings
from .transcript import assign_speakers, words_in_range

log = logging.getLogger(__name__)

MAX_SHORT = 180.0  # YouTube Shorts: max. 3 Minuten


class Cancelled(Exception):
    pass


def default_settings() -> dict:
    s = load_settings()
    return {
        "mode": "auto",                 # auto | single
        "model": transcribe.default_model(),
        "language": "de",
        "num_speakers": 2,
        "clip_count": 5,
        "min_len": 20,
        "max_len": 55,
        "use_claude": bool(api_key()),
        "auto_render": True,
        "range": None,                  # [start, end] in Sekunden (nur diesen Teil verarbeiten)
        "render": RenderOptions(watermark=s.get("watermark", ""), sign_text=s.get("sign_text", "")).to_dict(),
    }


# --------------------------------------------------------------------------- Hilfen

def speaker_names(p: dict) -> dict[int, str]:
    names = {}
    for sp in p.get("speakers", []):
        cid = sp.get("char")
        if cid and cid != "none":
            names[int(sp["spk"])] = get_character(cid).get("name", cid)
        elif sp.get("other"):
            names[int(sp["spk"])] = "Sonstige (Werbung/Einspieler)"
        else:
            names[int(sp["spk"])] = f"Sprecher {int(sp['spk']) + 1}"
    return names


def clip_words(store: ProjectStore, p: dict, clip: dict) -> list[dict]:
    """Wörter eines Clips (bearbeitete Fassung, falls vorhanden) in Projektzeit."""
    if clip.get("words"):
        return clip["words"]
    tr = store.transcript(p["id"]) or {"words": []}
    return words_in_range(tr["words"], clip["start"], clip["end"])


def seats_for(p: dict) -> list:
    chars = {c["id"]: get_character(c["id"]) for c in list_characters()}
    mapping = [sp for sp in p.get("speakers", []) if sp.get("char") in chars]
    return seats_from_mapping(mapping, chars)


def render_options(p: dict, clip: dict) -> RenderOptions:
    opts = dict(p["settings"].get("render") or {})
    opts.update(clip.get("render") or {})
    if "title" not in (clip.get("render") or {}):
        opts["title"] = clip.get("title", "")
    return RenderOptions.from_dict(opts)


def new_clip(start: float, end: float, title: str = "", reason: str = "", score: float = 0.0,
             source: str = "manuell") -> dict:
    return {"id": secrets.token_hex(3), "start": round(start, 2), "end": round(end, 2), "title": title,
            "reason": reason, "score": round(score, 2), "source": source, "status": "new", "progress": 0.0,
            "message": "", "video": None, "words": None, "render": {}, "created": time.time()}


# --------------------------------------------------------------------------- Schritte

class Pipeline:
    def __init__(self, store: ProjectStore):
        self.store = store

    def _progress(self, pid: str, step: str, base: float, span: float):
        def fn(frac: float, msg: str) -> None:
            self.store.set_live(pid, {"step": step, "progress": round(base + span * max(0.0, min(1.0, frac)), 4),
                                      "message": msg})
        return fn

    def process(self, pid: str, cancel: threading.Event) -> None:
        store = self.store
        p = store.get(pid)
        s = p["settings"]
        d = store.dir(pid)
        store.update(pid, lambda q: q.update(status="processing", error=None))

        def check():
            if cancel.is_set():
                raise Cancelled()

        # 1) Audio extrahieren
        prog = self._progress(pid, "Audio", 0.0, 0.05)
        prog(0.0, "Lese Datei und extrahiere die Tonspur …")
        info = media.probe(p["source"])
        if not info["has_audio"]:
            raise RuntimeError("Die Datei enthält keine Tonspur.")
        rng = s.get("range")
        start = float(rng[0]) if rng and rng[0] is not None else 0.0
        end = float(rng[1]) if rng and rng[1] is not None else None
        wav = d / "audio16k.wav"
        media.extract_audio_wav(p["source"], wav, start=start or None, end=end)
        audio, sr = media.read_wav(wav)
        duration = len(audio) / sr
        if duration < 2:
            raise RuntimeError("Die Tonspur ist zu kurz.")
        store.update(pid, lambda q: q.update(offset=start, duration=duration, source_info=info))
        np.save(d / "env10.npy", media.rms_envelope(audio, sr, 10))
        check()

        # 2) Spracherkennung
        prog = self._progress(pid, "Transkription", 0.05, 0.6)
        # Zwischenstände erlauben das Fortsetzen nach einem Abbruch (gleiches Modell, gleicher Bereich)
        partial_file = d / "transcript.partial.json"
        key = {"model": s.get("model", "small"), "range": s.get("range"), "language": s.get("language"),
               "duration": round(duration, 2)}
        resume = None
        if partial_file.exists():
            try:
                part = json.loads(partial_file.read_text(encoding="utf-8"))
                if part.get("key") == key:
                    resume = part
            except Exception:
                resume = None

        def save_partial(words: list, done_until: float) -> None:
            tmp = partial_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"key": key, "done_until": done_until, "words": words}), encoding="utf-8")
            tmp.replace(partial_file)

        tr = transcribe.transcribe(audio, s.get("model", "small"), s.get("language") or None,
                                   hotwords=load_settings().get("hotwords") or None,
                                   progress=lambda f, m: (prog(f, m), check()),
                                   resume=resume, checkpoint=save_partial)
        check()

        # 3) Sprecher erkennen
        prog = self._progress(pid, "Sprecher", 0.65, 0.2)
        num = int(s.get("num_speakers") or 0)
        if num == 1:
            segs, embs = [{"s": 0.0, "e": duration, "spk": 0}], {}
        else:
            segs, embs = diarize.diarize_isolated(wav, num, progress=prog, cancel=cancel)
        assign_speakers(tr["words"], segs)
        store.save_transcript(pid, {"language": tr["language"], "words": tr["words"], "diarization": segs})
        partial_file.unlink(missing_ok=True)
        prog(0.9, "Ordne Stimmen den Figuren zu …")
        speakers = self._speaker_table(audio, segs, tr["words"])
        self._auto_map(speakers, embs)
        (d / "speaker_embeddings.json").write_text(json.dumps({str(k): v for k, v in embs.items()}), encoding="utf-8")
        store.update(pid, lambda q: q.update(speakers=speakers))
        check()

        # 4) Clips finden
        prog = self._progress(pid, "Clips", 0.85, 0.15)
        p = store.get(pid)
        if s.get("mode") == "single" or duration <= max(30.0, float(s.get("max_len", 55))):
            words = tr["words"]
            c_start = max(0.0, (words[0]["s"] - 0.2) if words else 0.0)
            c_end = min(duration, (words[-1]["e"] + 0.4) if words else duration)
            if c_end - c_start > MAX_SHORT:
                store.update(pid, lambda q: q["notes"].append(
                    "Achtung: Der Clip ist länger als 3 Minuten (Shorts-Limit)."))
            title = highlights._local_title(words) if words else ""
            clips = [new_clip(c_start, c_end, title, "Gesamte Datei", 0, "ganze Datei")]
        else:
            self.find_clips(pid, cancel, prog)
            clips = None
        if clips is not None:
            store.update(pid, lambda q: q.update(clips=clips))
        store.update(pid, lambda q: q.update(status="ready"))
        store.set_live(pid, None)

    def rediarize(self, pid: str, cancel: threading.Event, num_speakers: int | None = None,
                  refind: bool = False) -> None:
        """Sprecher neu erkennen, ohne neu zu transkribieren."""
        store = self.store
        d = store.dir(pid)
        tr = store.transcript(pid)
        if not tr:
            raise RuntimeError("Noch kein Transkript vorhanden – bitte zuerst verarbeiten.")
        if num_speakers is not None:
            store.update(pid, lambda q: q["settings"].update(num_speakers=int(num_speakers)))
        num = int(store.get(pid)["settings"].get("num_speakers") or 0)
        prog = self._progress(pid, "Sprecher", 0.0, 0.85 if refind else 1.0)
        words = transcribe._merge_fragments([dict(w) for w in tr["words"]])
        if num == 1:
            dur = float(store.get(pid).get("duration") or (words[-1]["e"] if words else 0))
            segs, embs = [{"s": 0.0, "e": dur, "spk": 0}], {}
        else:
            segs, embs = diarize.diarize_isolated(d / "audio16k.wav", num, progress=prog, cancel=cancel)
        assign_speakers(words, segs)
        store.save_transcript(pid, {**tr, "words": words, "diarization": segs})
        speakers = self._speaker_table(None, segs, words)
        self._auto_map(speakers, embs)
        (d / "speaker_embeddings.json").write_text(json.dumps({str(k): v for k, v in embs.items()}), encoding="utf-8")

        def fn(q):
            q["speakers"] = speakers
            for c in q["clips"]:
                c["words"] = None  # alte Sprecher-Zuordnungen in bearbeiteten Untertiteln verwerfen
                if c.get("status") == "done":
                    c["stale"] = True
        store.update(pid, fn)
        if refind:
            self.find_clips(pid, cancel, self._progress(pid, "Clips", 0.85, 0.15), append=False)
        store.set_live(pid, None)

    def find_clips(self, pid: str, cancel: threading.Event, prog=None, count: int | None = None,
                   use_claude: bool | None = None, min_len: float | None = None, max_len: float | None = None,
                   append: bool = False) -> None:
        store = self.store
        p = store.get(pid)
        s = p["settings"]
        tr = store.transcript(pid)
        if not tr:
            raise RuntimeError("Noch kein Transkript vorhanden.")
        prog = prog or self._progress(pid, "Clips", 0.0, 1.0)
        env_file = store.dir(pid) / "env10.npy"
        if env_file.exists():
            env10 = np.load(env_file)
        else:
            audio, sr = media.read_wav(store.dir(pid) / "audio16k.wav")
            env10 = media.rms_envelope(audio, sr, 10)
            del audio
        words = tr["words"]
        existing = p["clips"] if append else []
        if existing:  # bereits vorhandene Bereiche nicht nochmal vorschlagen
            words_free = [w for w in words if not any(c["start"] - 1 <= w["s"] <= c["end"] + 1 for c in existing)]
        else:
            words_free = words
        sugg, note = highlights.find_clips(
            words_free, env10, speaker_names(p), count=int(count or s.get("clip_count", 5)),
            min_len=float(min_len or s.get("min_len", 20)), max_len=float(max_len or s.get("max_len", 55)),
            use_claude=bool(s.get("use_claude") if use_claude is None else use_claude), api_key=api_key(),
            context=load_settings().get("podcast_context", ""), progress=prog,
            avoid_speakers={sp["spk"] for sp in p.get("speakers", []) if sp.get("other") or sp.get("char") == "none"})
        clips = [new_clip(x.start, x.end, x.title, x.reason, x.score, x.source) for x in sugg]
        clips.sort(key=lambda c: c["start"])

        def fn(q):
            q["clips"] = (q["clips"] if append else []) + clips
            if note:
                q["notes"].append(note)
        store.update(pid, fn)

    def _speaker_table(self, audio: np.ndarray, segs: list[dict], words: list[dict]) -> list[dict]:
        talk: dict[int, float] = {}
        longest: dict[int, tuple[float, float]] = {}
        for sg in segs:
            k = sg["spk"]
            talk[k] = talk.get(k, 0.0) + sg["e"] - sg["s"]
            if k not in longest or sg["e"] - sg["s"] > longest[k][1] - longest[k][0]:
                longest[k] = (sg["s"], sg["e"])
        for w in words:  # Sprecher, die nur in Wörtern auftauchen
            talk.setdefault(w["spk"], 0.0)
        others = {sg["spk"] for sg in segs if sg.get("other")}
        table = []
        for k in sorted(talk, key=lambda k: (k in others, -talk[k])):
            s0, e0 = longest.get(k, (0.0, 0.0))
            sample = [round(s0, 2), round(min(e0, s0 + 7.0), 2)]
            text = " ".join(w["w"] for w in words if s0 <= w["s"] < sample[1])[:140]
            table.append({"spk": int(k), "char": None, "talk_time": round(talk[k], 1), "sample": sample,
                          "sample_text": text, "matched": False, "other": k in others})
        return table

    def _auto_map(self, speakers: list[dict], embs: dict[int, list[float]]) -> None:
        defaults = [c for c in load_settings().get("default_chars", ["rezo", "julien", "gast"])]
        available = {c["id"] for c in list_characters()}
        candidates = [c for c in defaults if c in available] + sorted(available - set(defaults))
        others = {sp["spk"] for sp in speakers if sp.get("other")}
        matched = diarize.match_profiles({k: v for k, v in embs.items() if k not in others}, candidates)
        used = set(matched.values())
        free = [c for c in candidates if c not in used]
        for sp in speakers:
            k = sp["spk"]
            if sp.get("other"):
                sp["char"] = "none"  # Werbung/Einspieler bekommen keine Figur
            elif k in matched:
                sp["char"] = matched[k]
                sp["matched"] = True
            elif free:
                sp["char"] = free.pop(0)
        # Sitzordnung: die Reihenfolge der Standardfiguren (z.B. Rezo links, Julien rechts)
        order = {c: i for i, c in enumerate(candidates)}
        speakers.sort(key=lambda sp: order.get(sp.get("char"), 99))

    def remember_voices(self, pid: str) -> int:
        p = self.store.get(pid)
        f = self.store.dir(pid) / "speaker_embeddings.json"
        if not f.exists():
            return 0
        embs = json.loads(f.read_text(encoding="utf-8"))
        n = 0
        for sp in p.get("speakers", []):
            cid = sp.get("char")
            e = embs.get(str(sp["spk"]))
            if cid and cid != "none" and e is not None and sp.get("talk_time", 0) >= 8:
                diarize.save_profile(cid, e)
                n += 1
        return n

    def render(self, pid: str, cid: str, cancel: threading.Event) -> None:
        store = self.store
        p = store.get(pid)
        clip = next(c for c in p["clips"] if c["id"] == cid)
        seats = seats_for(p)
        if not seats:
            raise RuntimeError("Keine Figuren zugeordnet – bitte bei 'Sprecher' Figuren auswählen.")
        opts = render_options(p, clip)
        words = clip_words(store, p, clip)
        out = store.dir(pid) / "clips" / f"{_safe(clip.get('title') or 'clip')}_{cid}.mp4"
        old = clip.get("video")
        store.update_clip(pid, cid, status="rendering", progress=0.0, message="Starte …", error=None)

        def prog(f: float, m: str) -> None:
            store.set_live(pid, {"step": "Rendern", "progress": f, "message": m, "clip": cid})
            if int(f * 100) % 5 == 0:
                store.update_clip(pid, cid, progress=round(f, 3), message=m)

        # Projektzeit -> Zeit in der Originaldatei (falls nur ein Bereich verarbeitet wurde)
        offset = float(p.get("offset") or 0.0)
        res = render_clip(p["source"], store.dir(pid) / "audio16k.wav", words, clip["start"], clip["end"], seats,
                          out, opts, progress=prog, cancel=cancel, source_offset=offset)
        rel = {k: str(Path(v).relative_to(store.dir(pid))) for k, v in res.items() if k in ("video", "srt", "thumb")}
        if old and old != rel["video"]:
            for ext in (".mp4", ".srt", ".jpg"):
                (store.dir(pid) / Path(old).with_suffix(ext)).unlink(missing_ok=True)
        store.update_clip(pid, cid, status="done", progress=1.0, message="Fertig", video=rel["video"],
                          srt=rel["srt"], thumb=rel["thumb"], rendered=time.time(), stale=False, error=None)
        store.set_live(pid, None)

    def preview(self, pid: str, cid: str, at: float | None) -> bytes:
        p = self.store.get(pid)
        clip = next(c for c in p["clips"] if c["id"] == cid)
        return render_preview(self.store.dir(pid) / "audio16k.wav", clip_words(self.store, p, clip),
                              clip["start"], clip["end"], seats_for(p), render_options(p, clip), at)


def _safe(name: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in name).strip().replace(" ", "_")
    return keep[:40] or "clip"


# --------------------------------------------------------------------------- Warteschlange

class Worker:
    """Arbeitet Aufträge nacheinander in einem Hintergrund-Thread ab."""

    def __init__(self, store: ProjectStore):
        self.store = store
        self.pipeline = Pipeline(store)
        self.q: queue.Queue = queue.Queue()
        self.current: dict | None = None
        self.cancel = threading.Event()
        self.pending: list[dict] = []
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True, name="worker")
        self.thread.start()

    def submit(self, kind: str, pid: str, **kw) -> None:
        job = {"kind": kind, "pid": pid, **kw}
        with self._lock:
            self.pending.append(job)
        if kind == "render":
            self.store.update_clip(pid, kw["cid"], status="queued", message="In der Warteschlange")
        self.q.put(job)

    def cancel_project(self, pid: str) -> None:
        with self._lock:
            for job in list(self.pending):
                if job["pid"] == pid:
                    job["cancelled"] = True
            if self.current and self.current["pid"] == pid:
                self.cancel.set()

    def status(self) -> dict:
        with self._lock:
            return {"current": self.current, "pending": [{k: v for k, v in j.items() if k != "cancelled"}
                                                         for j in self.pending if not j.get("cancelled")]}

    def _run(self) -> None:
        while True:
            job = self.q.get()
            with self._lock:
                if job in self.pending:
                    self.pending.remove(job)
                if job.get("cancelled"):
                    if job["kind"] == "render":
                        self._safe_update_clip(job, status="new", message="Abgebrochen")
                    continue
                self.current = job
                self.cancel.clear()
            try:
                if self.store.get(job["pid"]) is None:
                    continue
                if job["kind"] == "process":
                    self.pipeline.process(job["pid"], self.cancel)
                    p = self.store.get(job["pid"])
                    if p and p["settings"].get("auto_render"):
                        for c in p["clips"]:
                            self.submit("render", job["pid"], cid=c["id"])
                elif job["kind"] == "render":
                    self.pipeline.render(job["pid"], job["cid"], self.cancel)
                elif job["kind"] == "rediarize":
                    self.pipeline.rediarize(job["pid"], self.cancel, num_speakers=job.get("num_speakers"),
                                            refind=job.get("refind", False))
                    p = self.store.get(job["pid"])
                    if job.get("refind") and p and p["settings"].get("auto_render"):
                        for c in p["clips"]:
                            self.submit("render", job["pid"], cid=c["id"])
                elif job["kind"] == "find_clips":
                    self.pipeline.find_clips(job["pid"], self.cancel, count=job.get("count"),
                                             use_claude=job.get("use_claude"), min_len=job.get("min_len"),
                                             max_len=job.get("max_len"), append=job.get("append", True))
                    self.store.set_live(job["pid"], None)
            except Exception as exc:
                cancelled = isinstance(exc, Cancelled) or "Abgebrochen" in str(exc)
                if not cancelled:
                    log.error("Auftrag fehlgeschlagen: %s\n%s", exc, traceback.format_exc())
                msg = "Abgebrochen" if cancelled else str(exc)
                if job["kind"] == "render":
                    self._safe_update_clip(job, status="new" if cancelled else "error", message=msg, error=msg)
                elif self.store.get(job["pid"]) is not None:
                    self.store.update(job["pid"], lambda q: q.update(status="error", error=msg))
                self.store.set_live(job["pid"], None)
            finally:
                with self._lock:
                    self.current = None

    def _safe_update_clip(self, job: dict, **fields) -> None:
        try:
            self.store.update_clip(job["pid"], job["cid"], **fields)
        except Exception:
            pass
