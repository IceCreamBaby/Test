"""ffmpeg-Hilfsfunktionen (ffmpeg wird über imageio-ffmpeg mitgeliefert)."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import wave
from functools import lru_cache
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000

# Unter Windows kein schwarzes Konsolenfenster für jeden ffmpeg-Aufruf öffnen.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - Fallback auf System-ffmpeg
        exe = shutil.which("ffmpeg")
        if not exe:
            raise RuntimeError("ffmpeg wurde nicht gefunden. Bitte 'imageio-ffmpeg' installieren.")
        return exe


def run_ffmpeg(args: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostdin", *args]
    proc = subprocess.run(cmd, input=input_bytes, capture_output=True, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")[-2000:]
        raise RuntimeError(f"ffmpeg-Fehler:\n{err}")
    return proc


def popen_ffmpeg(args: list[str], **kwargs) -> subprocess.Popen:
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostdin", *args]
    return subprocess.Popen(cmd, creationflags=_NO_WINDOW, **kwargs)


def probe(path: str | Path) -> dict:
    """Liest Dauer und vorhandene Streams aus der ffmpeg-Ausgabe."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True,
        creationflags=_NO_WINDOW,
    )
    text = proc.stderr.decode("utf-8", "replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    duration = 0.0
    if m:
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    has_audio = bool(re.search(r"Stream #.*Audio:", text))
    has_video = any("attached pic" not in line for line in re.findall(r"Stream #.*Video:.*", text))
    return {"duration": duration, "has_audio": has_audio, "has_video": has_video}


def extract_audio_wav(src: str | Path, dst: str | Path, start: float | None = None,
                      end: float | None = None, sr: int = SAMPLE_RATE) -> None:
    """Extrahiert die Tonspur als 16-bit Mono-WAV (Standard: 16 kHz für die Spracherkennung)."""
    args: list[str] = ["-y"]
    if start:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(src)]
    if end is not None:
        args += ["-t", f"{max(0.1, end - (start or 0.0)):.3f}"]
    args += ["-vn", "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(dst)]
    run_ffmpeg(args)


def read_wav(path: str | Path, start: float = 0.0, end: float | None = None) -> tuple[np.ndarray, int]:
    """Liest (einen Ausschnitt aus) einer 16-bit-PCM-WAV als float32 im Bereich [-1, 1]."""
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        a = max(0, int(start * sr))
        b = n if end is None else min(n, int(end * sr))
        w.setpos(min(a, n))
        raw = w.readframes(max(0, b - a))
        ch = w.getnchannels()
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    data *= 1.0 / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr


def wav_duration(path: str | Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def write_wav(path: str | Path, data: np.ndarray, sr: int) -> None:
    pcm = np.clip(data, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def rms_envelope(audio: np.ndarray, sr: int, fps: float) -> np.ndarray:
    """Lautstärke-Hüllkurve mit einem Wert pro Videoframe (0..1, robust normalisiert).

    Arbeitet blockweise, damit auch mehrstündige Podcasts wenig Arbeitsspeicher brauchen.
    """
    hop = sr / fps
    n = int(np.ceil(len(audio) / hop)) if len(audio) else 0
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    bounds = np.minimum((np.arange(n + 1) * hop).astype(np.int64), len(audio))
    bounds[-1] = len(audio)
    energy = np.zeros(n, dtype=np.float64)
    step = max(1, int(60 * fps))
    for i in range(0, n, step):
        j = min(n, i + step)
        a, b = int(bounds[i]), int(bounds[j])
        if b <= a:
            continue
        seg = np.asarray(audio[a:b], dtype=np.float32)
        idx = bounds[i:j] - a
        valid = idx < (b - a)
        sums = np.zeros(j - i, dtype=np.float64)
        sums[valid] = np.add.reduceat(seg * seg, idx[valid])
        counts = np.diff(np.append(idx, b - a))
        energy[i:j] = sums / np.maximum(counts, 1)
    env = np.sqrt(np.convolve(energy, [0.25, 0.5, 0.25], mode="same")).astype(np.float32)
    nz = env[env > 1e-4]
    ref = float(np.percentile(nz, 95)) if len(nz) else 1.0
    return np.clip(env / max(ref, 1e-4), 0.0, 1.5)


def format_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:04.1f}"
    return f"{m}:{s:04.1f}"


def parse_ts(text: str | float | int | None) -> float | None:
    """'1:02:03.5', '62:03', '3723' -> Sekunden."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    text = text.strip().replace(",", ".")
    if not text:
        return None
    parts = text.split(":")
    try:
        vals = [float(p) for p in parts]
    except ValueError as exc:
        raise ValueError(f"Ungültige Zeitangabe: {text!r}") from exc
    total = 0.0
    for v in vals:
        total = total * 60 + v
    return total
