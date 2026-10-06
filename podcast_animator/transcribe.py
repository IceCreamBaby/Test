"""Spracherkennung mit faster-whisper (lokal, Wort-Zeitstempel)."""
from __future__ import annotations

import logging
import os
from typing import Callable

import numpy as np

from .media import SAMPLE_RATE
from .paths import MODELS_DIR

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

# Anzeigename -> faster-whisper Modell
MODEL_CHOICES = {
    "base": "Sehr schnell (ungenau)",
    "small": "Schnell (ordentlich)",
    "medium": "Langsam (gut)",
    "large-v3-turbo": "Beste Qualität (empfohlen mit Grafikkarte)",
}


def cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def default_model() -> str:
    return "large-v3-turbo" if cuda_available() else "small"


def _load_model(model_size: str, device: str):
    from faster_whisper import WhisperModel

    compute_type = "float16" if device == "cuda" else "int8"
    threads = max(4, min(16, (os.cpu_count() or 4) // 2))
    return WhisperModel(model_size, device=device, compute_type=compute_type, cpu_threads=threads,
                        download_root=str(MODELS_DIR / "whisper"))


CheckpointFn = Callable[[list, float], None]


def transcribe(audio: np.ndarray, model_size: str = "small", language: str | None = "de",
               device: str = "auto", hotwords: str | None = None,
               progress: ProgressFn | None = None, resume: dict | None = None,
               checkpoint: CheckpointFn | None = None) -> dict:
    """Gibt {'language', 'words': [{w, s, e, p}]} zurück. Zeiten in Sekunden relativ zu `audio`.

    resume: {'words': [...], 'done_until': Sekunden} – setzt eine abgebrochene Transkription fort.
    checkpoint(words, done_until) wird regelmäßig mit dem Zwischenstand aufgerufen.
    """
    devices = ["cuda", "cpu"] if device == "auto" and cuda_available() else [device if device != "auto" else "cpu"]
    last_error: Exception | None = None
    for dev in devices:
        try:
            return _transcribe_on(audio, model_size, language, dev, hotwords, progress, resume, checkpoint)
        except Exception as exc:  # z.B. fehlende CUDA-Bibliotheken -> CPU versuchen
            last_error = exc
            if dev == "cuda":
                log.warning("Transkription auf der GPU fehlgeschlagen (%s), wechsle auf CPU.", exc)
                if progress:
                    progress(0.0, "GPU nicht nutzbar – transkribiere mit CPU …")
                continue
            raise
    raise RuntimeError(f"Transkription fehlgeschlagen: {last_error}")


def _transcribe_on(audio, model_size, language, device, hotwords, progress, resume, checkpoint) -> dict:
    if progress:
        progress(0.0, f"Lade Spracherkennungsmodell '{model_size}' ({device.upper()}) – beim ersten Mal wird es heruntergeladen …")
    model = _load_model(model_size, device)
    duration = len(audio) / SAMPLE_RATE
    words: list[dict] = []
    offset = 0.0
    if resume and resume.get("words") is not None:
        offset = max(0.0, min(float(resume.get("done_until", 0.0)), duration))
        words = [dict(w) for w in resume["words"] if w["e"] <= offset + 0.01]
        if progress:
            progress(offset / duration, f"Setze Transkription bei {_fmt(offset)} fort …")
    segments, info = model.transcribe(
        audio[int(offset * SAMPLE_RATE):],
        language=language or None,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        beam_size=5,
        condition_on_previous_text=False,
        hotwords=hotwords or None,
    )
    last_checkpoint = offset
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if not text:
                continue
            words.append({"w": text, "s": round(float(w.start) + offset, 3), "e": round(float(w.end) + offset, 3),
                          "p": round(float(w.probability), 3)})
        done = seg.end + offset
        if progress and duration > 0:
            progress(min(1.0, done / duration), f"Transkribiere … {_fmt(done)} / {_fmt(duration)}")
        # Zwischenstand sichern (Segmentende = sinnvoller Wiedereinstiegspunkt)
        if checkpoint and done - last_checkpoint >= 90:
            checkpoint(words, done)
            last_checkpoint = done
    words = _merge_fragments(words)
    _fix_word_times(words)
    return {"language": info.language, "words": words}


def _merge_fragments(words: list[dict]) -> list[dict]:
    """Fügt abgetrennte Wortteile wieder an („Nano“ + „-Bots“ → „Nano-Bots“)."""
    out: list[dict] = []
    for w in words:
        if out and len(w["w"]) > 1 and w["w"][0] in "-'’" and w["s"] - out[-1]["e"] < 0.35:
            prev = out[-1]
            prev["w"] = prev["w"] + w["w"]
            prev["e"] = w["e"]
            prev["p"] = min(prev.get("p", 1.0), w.get("p", 1.0))
        else:
            out.append(w)
    return out


def _fix_word_times(words: list[dict]) -> None:
    """Sorgt für monotone, nicht überlappende Zeitstempel mit Mindestdauer."""
    for i, w in enumerate(words):
        if i > 0 and w["s"] < words[i - 1]["e"]:
            w["s"] = words[i - 1]["e"]
        if w["e"] < w["s"] + 0.05:
            w["e"] = round(w["s"] + 0.05, 3)


def _fmt(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"
