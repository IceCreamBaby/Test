"""Setzt alles zusammen: rendert die Frames mit skia und kodiert sie mit ffmpeg zu einem MP4 (inkl. Ton)."""
from __future__ import annotations

import logging
import math
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import skia

from .. import media
from ..transcript import shift_words, to_srt, words_in_range
from .animate import Animator
from .characters import make_character
from .draw import color, fill
from .overlay import SubtitleStyle, Subtitles, dim, draw_title, draw_watermark
from .scene import (DESK_Y, H, THEMES, W, build_layers, camera_for, clamp_camera, emphasis_events, make_slots,
                    plan_shots)

log = logging.getLogger(__name__)
ProgressFn = Callable[[float, str], None]

@dataclass
class RenderOptions:
    layout: str = "studio"            # studio | split
    theme: str = "lila"
    fps: int = 30
    title: str = ""
    title_mode: str = "always"        # always | start | off
    sign_text: str = "PODCAST"
    watermark: str = "@xxforcegamingxx · Fan-Animation"
    subtitles: bool = True
    uppercase: bool = False
    show_names: bool = True
    max_words: int = 3
    liveliness: float = 1.0
    crf: int = 20
    preset: str = "veryfast"
    loudnorm: bool = True

    @classmethod
    def from_dict(cls, d: dict | None) -> "RenderOptions":
        d = d or {}
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Seat:
    """Eine Figur am Tisch und welche erkannten Sprecher-Nummern zu ihr gehören."""
    style: dict
    speakers: list[int] = field(default_factory=list)


class ClipRenderer:
    def __init__(self, words_local: list[dict], env: np.ndarray, duration: float, seats: list[Seat],
                 opts: RenderOptions):
        self.opts = opts
        self.fps = opts.fps
        self.duration = duration
        self.n = max(1, int(round(duration * self.fps)))
        self.seats = seats
        self.slot_of_spk = {spk: i for i, seat in enumerate(seats) for spk in seat.speakers}
        # Wörter von nicht zugeordneten Sprechern (z.B. Werbung/Intro) bekommen keine Figur, bleiben aber im Untertitel
        self.words = words_local
        self.slots = make_slots(len(seats))
        self.characters = [make_character(seat.style) for seat in seats]
        theme = THEMES.get(opts.theme, THEMES["lila"])
        self.theme = theme
        self.layers = build_layers(opts.theme, self.slots, opts.sign_text,
                                   with_mic=[bool(seat.style.get("mic", True)) for seat in seats])
        self.env = env
        self.anim = Animator(words_local, env, self.fps, self.n, self.slot_of_spk,
                             [s.inner for s in self.slots], liveliness=opts.liveliness)
        segs = [(w["s"], w["e"], self.slot_of_spk[w["spk"]]) for w in words_local if w.get("spk") in self.slot_of_spk]
        self.shots = plan_shots(segs, duration, len(seats))
        self.punch = self._punch_track()
        colors = {}
        names = {}
        for spk, slot in self.slot_of_spk.items():
            colors[spk] = seats[slot].style.get("color", "#ffd400")
            names[spk] = seats[slot].style.get("name", "")
        self.subs = Subtitles(words_local, colors, names, SubtitleStyle(
            uppercase=opts.uppercase, show_names=opts.show_names and len(seats) > 1, max_words=opts.max_words,
            y=960 if self._split else 1505))

    @property
    def _split(self) -> bool:
        return self.opts.layout == "split" and len(self.seats) == 2

    def _punch_track(self) -> np.ndarray:
        track = np.ones(self.n, dtype=np.float32)
        for e in emphasis_events(self.env, self.fps):
            for k in range(int(0.7 * self.fps)):
                f = e + k
                if f >= self.n:
                    break
                t = k / self.fps
                v = min(1.0, t / 0.07) * math.exp(-max(0.0, t - 0.07) / 0.25)
                track[f] = max(track[f], 1.0 + 0.07 * v)
        return track

    def _shot_at(self, t: float):
        for sh in self.shots:
            if sh.start <= t < sh.end:
                return sh
        return self.shots[-1]

    # ---------------------------------------------------------------- Zeichnen
    def _draw_world(self, c: skia.Canvas, states, zoom: float) -> None:
        for layer in self.layers["bg"]:
            layer.draw(c, zoom)
        for slot, ch, st in zip(self.slots, self.characters, states):
            c.save()
            c.translate(slot.x, DESK_Y)
            c.scale(slot.scale, slot.scale)
            ch.draw_body(c, st)
            c.restore()
        for layer in self.layers["desk"]:
            layer.draw(c, zoom)
        for slot, ch, st in zip(self.slots, self.characters, states):
            c.save()
            c.translate(slot.x, DESK_Y)
            c.scale(slot.scale, slot.scale)
            ch.draw_front(c, st, slot.inner)
            c.restore()
        for layer in self.layers["mics"]:
            layer.draw(c, zoom)

    def draw_frame(self, c: skia.Canvas, f: int) -> None:
        t = f / self.fps
        states = [self.anim.state(s, f) for s in range(len(self.seats))]
        c.clear(color("#000000"))
        laugh = max((st.laugh for st in states), default=0.0)
        shake_x = math.sin(t * 31) * 3 * laugh
        shake_y = math.cos(t * 27) * 2 * laugh
        if self._split:
            for i in range(2):
                rect = skia.Rect.MakeXYWH(0, i * H / 2, W, H / 2)
                slot = self.slots[i]
                hx, hy = slot.head
                speaking = float(self.anim.talk[i, f] > 0.05 or self.anim.speaking[i, f])
                z = 1.78 * (self.punch[f] if speaking else 1.0)
                # Kopf etwas tiefer im Panel: oben Platz für den Titel, in der Mitte für die Untertitel
                offset = -10 if i == 0 else 30
                cx, cy, z = clamp_camera(hx + shake_x, hy + offset * slot.scale + shake_y, z, W, H / 2)
                c.save()
                c.clipRect(rect)
                c.translate(W / 2, rect.centerY())
                c.scale(z, z)
                c.translate(-cx, -cy)
                self._draw_world(c, states, z)
                c.restore()
                cur = self.anim.current_speaker[f]
                dim(c, rect, 0.0 if cur in (-1, i) else 0.3)
            c.drawRect(skia.Rect.MakeXYWH(0, H / 2 - 7, W, 14), fill("#0b0b0f"))
            c.drawRect(skia.Rect.MakeXYWH(0, H / 2 - 3, W, 6), fill(self.theme["accent"]))
        else:
            shot = self._shot_at(t)
            cx, cy, z = camera_for(shot, self.slots, t)
            cx, cy, z = clamp_camera(cx + shake_x, cy + shake_y, z * float(self.punch[f]))
            c.save()
            c.translate(W / 2, H / 2)
            c.scale(z, z)
            c.translate(-cx, -cy)
            self._draw_world(c, states, z)
            c.restore()
        o = self.opts
        if o.title and (o.title_mode == "always" or (o.title_mode == "start" and t < 4.0)):
            c.save()
            if o.title_mode == "start" and t > 3.6:
                c.translate(0, -400 * (t - 3.6) / 0.4)
            draw_title(c, o.title, t, y=150 if self._split else 250, accent=self.theme["accent"],
                       small=self._split)
            c.restore()
        if o.subtitles:
            self.subs.draw(c, t)
        if o.watermark:
            draw_watermark(c, o.watermark, y=1880 if self._split else 1690)

    def snapshot(self, f: int) -> skia.Image:
        surf = skia.Surface(W, H)
        self.draw_frame(surf.getCanvas(), f)
        return surf.makeImageSnapshot()


# --------------------------------------------------------------------------- öffentliche API

def seats_from_mapping(mapping: list[dict], characters: dict[str, dict]) -> list[Seat]:
    """mapping: [{'spk': 0, 'char': 'rezo'}, ...] in Sitzreihenfolge (links -> rechts)."""
    seats: list[Seat] = []
    by_char: dict[str, Seat] = {}
    for m in mapping:
        cid = m.get("char")
        if not cid or cid == "none":
            continue
        if cid not in by_char:
            by_char[cid] = Seat(style=characters[cid], speakers=[])
            seats.append(by_char[cid])
        by_char[cid].speakers.append(int(m["spk"]))
    return seats


def clip_inputs(audio_wav: str | Path, words: list[dict], start: float, end: float, fps: int) -> tuple[list[dict], np.ndarray]:
    """Wörter (relativ zum Clipstart) und Lautstärke-Hüllkurve eines Ausschnitts."""
    audio, sr = media.read_wav(audio_wav, start, end)
    env = media.rms_envelope(audio, sr, fps)
    local = shift_words(words_in_range(words, start, end), start)
    local = [w for w in local if w["e"] > 0 and w["s"] < end - start]
    return local, env


def prepare(audio_wav: str | Path, words: list[dict], start: float, end: float, seats: list[Seat],
            opts: RenderOptions) -> ClipRenderer:
    local, env = clip_inputs(audio_wav, words, start, end, opts.fps)
    return ClipRenderer(local, env, end - start, seats, opts)


def _video_args(fps: int, opts: RenderOptions) -> list[str]:
    return ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-"]


def _x264_args(fps: int, opts: RenderOptions) -> list[str]:
    return ["-c:v", "libx264", "-preset", opts.preset, "-crf", str(opts.crf), "-pix_fmt", "yuv420p",
            "-profile:v", "high", "-r", str(fps)]


def _encode_frames(r: ClipRenderer, frames: range, ffmpeg_args: list[str], tick: Callable[[int], None],
                   cancelled: Callable[[], bool]) -> None:
    """Zeichnet die Frames und schiebt sie in einen ffmpeg-Prozess (mit Doppelpuffer im Hintergrund)."""
    proc = media.popen_ffmpeg(ffmpeg_args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr_chunks: list[bytes] = []
    err_thread = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    err_thread.start()
    buffers = [np.empty((H, W, 4), dtype=np.uint8) for _ in range(3)]
    surfaces = [skia.Surface(b) for b in buffers]
    q: queue.Queue = queue.Queue(maxsize=1)
    write_error: list[Exception] = []

    def writer():
        while True:
            item = q.get()
            if item is None:
                break
            try:
                proc.stdin.write(memoryview(buffers[item]))
            except Exception as exc:  # ffmpeg abgestürzt
                write_error.append(exc)
                break

    wt = threading.Thread(target=writer, daemon=True)
    wt.start()
    stop = False
    try:
        for k, f in enumerate(frames):
            if write_error or (k % 10 == 0 and cancelled()):
                stop = True
                break
            i = k % 3
            r.draw_frame(surfaces[i].getCanvas(), f)
            q.put(i)
            tick(1)
    finally:
        q.put(None)
        wt.join()
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.wait()
        err_thread.join(timeout=5)
    if stop and not write_error:
        raise RuntimeError("Abgebrochen")
    if proc.returncode != 0 or write_error:
        err = b"".join(stderr_chunks).decode("utf-8", "replace")[-1500:]
        raise RuntimeError(f"Video-Kodierung fehlgeschlagen: {err or write_error}")


def _segment_worker(payload: dict, a: int, b: int, seg_path: str, progress_q, cancel_evt) -> str:
    """Läuft in einem eigenen Prozess: rendert die Frames [a, b) als stummes Video-Segment."""
    seats = [Seat(style=s["style"], speakers=s["speakers"]) for s in payload["seats"]]
    opts = RenderOptions.from_dict(payload["opts"])
    r = ClipRenderer(payload["words"], np.asarray(payload["env"], dtype=np.float32), payload["duration"], seats, opts)
    pending = [0]

    def tick(n: int) -> None:
        pending[0] += n
        if pending[0] >= 10:
            progress_q.put(pending[0])
            pending[0] = 0

    args = ["-y", "-loglevel", "error", *_video_args(r.fps, opts), *_x264_args(r.fps, opts), "-an", seg_path]
    _encode_frames(r, range(a, b), args, tick, lambda: cancel_evt.is_set())
    if pending[0]:
        progress_q.put(pending[0])
    return seg_path


def default_workers() -> int:
    cpu = os.cpu_count() or 2
    return max(1, min(8, cpu - 1))


def render_clip(source: str | Path, audio_wav: str | Path, words: list[dict], start: float, end: float,
                seats: list[Seat], out_path: str | Path, opts: RenderOptions,
                progress: ProgressFn | None = None, cancel: threading.Event | None = None,
                workers: int | None = None, source_offset: float = 0.0) -> dict:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if progress:
        progress(0.0, "Bereite Animation vor …")
    words_local, env = clip_inputs(audio_wav, words, start, end, opts.fps)
    duration = end - start
    fps = opts.fps
    n = max(1, int(round(duration * fps)))
    dur = n / fps
    workers = workers or default_workers()
    workers = max(1, min(workers, n // 90))  # Segmente nicht kürzer als ~3 Sekunden
    tmp = out_path.with_suffix(".tmp.mp4")
    afilters = ["afade=t=in:d=0.04", f"afade=t=out:st={max(0.0, dur - 0.12):.3f}:d=0.12"]
    if opts.loudnorm:
        afilters.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    audio_in = ["-ss", f"{start + source_offset:.3f}", "-t", f"{dur:.3f}", "-i", str(source)]
    audio_out = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-af", ",".join(afilters)]
    done = [0]
    t_start = time.time()

    def report(k: int) -> None:
        done[0] += k
        if progress and (done[0] % 15 < k or done[0] >= n):
            el = time.time() - t_start
            eta = el / max(1, done[0]) * (n - done[0])
            progress(min(0.99, done[0] / n), f"Rendere Animation … {done[0]}/{n} Bilder (noch ca. {int(eta)} s)")

    is_cancelled = (lambda: cancel is not None and cancel.is_set())
    if workers == 1:
        r = ClipRenderer(words_local, env, duration, seats, opts)
        args = ["-y", "-loglevel", "error", *_video_args(fps, opts), *audio_in, "-map", "0:v:0", "-map", "1:a:0?",
                *_x264_args(fps, opts), *audio_out, "-movflags", "+faststart", str(tmp)]
        try:
            _encode_frames(r, range(n), args, report, is_cancelled)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    else:
        r = None
        _render_parallel(words_local, env, duration, seats, opts, n, workers, out_path, tmp, audio_in, audio_out,
                         report, cancel)
    tmp.replace(out_path)
    # Untertitel-Datei und Vorschaubild
    if r is None:
        r = ClipRenderer(words_local, env, duration, seats, opts)
    names = {spk: r.seats[slot].style.get("name", "") for spk, slot in r.slot_of_spk.items()}
    srt_path = out_path.with_suffix(".srt")
    srt_path.write_text(to_srt(r.words, names), encoding="utf-8")
    thumb_path = out_path.with_suffix(".jpg")
    img = r.snapshot(min(r.n - 1, int(r.n * 0.3)))
    img.save(str(thumb_path), skia.kJPEG, 88)
    if progress:
        progress(1.0, f"Fertig in {int(time.time() - t_start)} s")
    return {"video": str(out_path), "srt": str(srt_path), "thumb": str(thumb_path), "duration": dur}


def _render_parallel(words_local, env, duration, seats, opts, n, workers, out_path: Path, tmp: Path,
                     audio_in, audio_out, report, cancel) -> None:
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor, wait, FIRST_EXCEPTION

    payload = {"words": words_local, "env": np.asarray(env, dtype=np.float32).tolist(), "duration": duration,
               "seats": [{"style": s.style, "speakers": s.speakers} for s in seats], "opts": opts.to_dict()}
    bounds = [round(n * i / workers) for i in range(workers + 1)]
    seg_dir = out_path.parent / f".{out_path.stem}_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    seg_paths = [str(seg_dir / f"seg_{i:02d}.mp4") for i in range(workers)]
    ctx = mp.get_context("spawn")
    manager = ctx.Manager()
    try:
        progress_q = manager.Queue()
        cancel_evt = manager.Event()
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
            futures = [ex.submit(_segment_worker, payload, bounds[i], bounds[i + 1], seg_paths[i], progress_q,
                                 cancel_evt) for i in range(workers)]
            pending = set(futures)
            while pending:
                finished, pending = wait(pending, timeout=0.3, return_when=FIRST_EXCEPTION)
                while not progress_q.empty():
                    report(progress_q.get())
                if cancel is not None and cancel.is_set():
                    cancel_evt.set()
                for fut in finished:
                    if fut.exception() is not None:
                        cancel_evt.set()
                        for other in pending:
                            other.cancel()
                        raise fut.exception()
            while not progress_q.empty():
                report(progress_q.get())
        list_file = seg_dir / "segments.txt"
        list_file.write_text("".join(f"file '{Path(p).name}'\n" for p in seg_paths), encoding="utf-8")
        media.run_ffmpeg(["-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_file),
                          *audio_in, "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy", *audio_out,
                          "-movflags", "+faststart", str(tmp)])
    finally:
        manager.shutdown()
        shutil.rmtree(seg_dir, ignore_errors=True)


def render_preview(audio_wav: str | Path | None, words: list[dict], start: float, end: float, seats: list[Seat],
                   opts: RenderOptions, at: float | None = None) -> bytes:
    """Einzelbild als JPEG (für die Vorschau im Browser)."""
    if audio_wav is None:
        dur = max(1.0, end - start)
        r = ClipRenderer(shift_words(words_in_range(words, start, end), start), np.zeros(int(dur * opts.fps)),
                         dur, seats, opts)
    else:
        r = prepare(audio_wav, words, start, end, seats, opts)
    f = int(((at if at is not None else (end - start) * 0.3)) * r.fps)
    img = r.snapshot(max(0, min(r.n - 1, f)))
    return bytes(img.encodeToData(skia.kJPEG, 85))


def character_preview(style: dict, theme: str = "lila", sign_text: str = "PODCAST", scale: float = 0.5) -> bytes:
    """Vorschaubild einer einzelnen Figur im Studio (für den Figuren-Editor)."""
    fps = 30
    words = [{"w": "Hallo!", "s": 0.1, "e": 0.9, "spk": 0, "p": 1.0}]
    env = np.full(fps, 0.9, dtype=np.float32)
    opts = RenderOptions(theme=theme, sign_text=sign_text, subtitles=False, watermark="", title="", fps=fps)
    r = ClipRenderer(words, env, 1.0, [Seat(style=style, speakers=[0])], opts)
    surf = skia.Surface(int(W * scale), int(H * scale))
    c = surf.getCanvas()
    c.scale(scale, scale)
    r.draw_frame(c, 12)
    return bytes(surf.makeImageSnapshot().encodeToData(skia.kJPEG, 85))
