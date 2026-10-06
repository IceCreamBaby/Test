"""Sprechererkennung (wer spricht wann?) mit sherpa-onnx + Stimmprofile zur automatischen Zuordnung."""
from __future__ import annotations

import json
import logging
import os
from typing import Callable

import numpy as np

from .media import SAMPLE_RATE
from .paths import MODELS_DIR, PROFILES_DIR

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

SEGMENTATION_REPO = "csukuangfj/sherpa-onnx-pyannote-segmentation-3-0"
EMBEDDING_REPO = "csukuangfj/speaker-embedding-models"
EMBEDDING_FILE = "wespeaker_en_voxceleb_resnet34_LM.onnx"
PROFILE_FILE = PROFILES_DIR / "profiles.json"
MATCH_THRESHOLD = 0.45


def _threads() -> int:
    return max(1, min(8, (os.cpu_count() or 4)))


def ensure_models(progress: ProgressFn | None = None) -> tuple[str, str]:
    from huggingface_hub import hf_hub_download

    if progress:
        progress(0.0, "Lade Modelle für die Sprechererkennung (einmalig) …")
    cache = str(MODELS_DIR / "diarization")
    seg = hf_hub_download(SEGMENTATION_REPO, "model.onnx", cache_dir=cache)
    emb = hf_hub_download(EMBEDDING_REPO, EMBEDDING_FILE, cache_dir=cache)
    return seg, emb


def diarize(audio: np.ndarray, num_speakers: int = 2, progress: ProgressFn | None = None) -> list[dict]:
    """Gibt eine nach Startzeit sortierte Liste [{s, e, spk}] zurück."""
    import sherpa_onnx

    seg_model, emb_model = ensure_models(progress)
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=seg_model),
            num_threads=_threads(),
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb_model, num_threads=_threads()),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=num_speakers if num_speakers and num_speakers > 0 else -1, threshold=0.5),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise RuntimeError("Konfiguration der Sprechererkennung ist ungültig.")
    sd = sherpa_onnx.OfflineSpeakerDiarization(config)

    def cb(done: int, total: int) -> int:
        if progress and total:
            progress(done / total, f"Erkenne Sprecher … {int(100 * done / total)} %")
        return 0

    if progress:
        progress(0.0, "Erkenne Sprecher …")
    result = sd.process(np.ascontiguousarray(audio, dtype=np.float32), callback=cb).sort_by_start_time()
    segs = [{"s": round(float(r.start), 3), "e": round(float(r.end), 3), "spk": int(r.speaker)} for r in result]
    return _renumber_by_talk_time(segs)


def _renumber_by_talk_time(segs: list[dict]) -> list[dict]:
    """Sprecher 0 = wer am meisten redet (stabile, nachvollziehbare Nummerierung)."""
    talk: dict[int, float] = {}
    for s in segs:
        talk[s["spk"]] = talk.get(s["spk"], 0.0) + s["e"] - s["s"]
    order = {spk: i for i, spk in enumerate(sorted(talk, key=lambda k: -talk[k]))}
    for s in segs:
        s["spk"] = order[s["spk"]]
    return segs


# --------------------------------------------------------------------------- Stimmprofile

def _extractor():
    import sherpa_onnx

    _, emb_model = ensure_models()
    return sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb_model, num_threads=_threads()))


def speaker_embeddings(audio: np.ndarray, segs: list[dict], max_seconds: float = 40.0) -> dict[int, list[float]]:
    """Mittleres Stimm-Embedding pro Sprecher aus seinen längsten Abschnitten."""
    extractor = _extractor()
    out: dict[int, list[float]] = {}
    for spk in sorted({s["spk"] for s in segs}):
        mine = sorted((s for s in segs if s["spk"] == spk), key=lambda s: -(s["e"] - s["s"]))
        embs, used = [], 0.0
        for s in mine:
            if used >= max_seconds or s["e"] - s["s"] < 1.0:
                break
            chunk = audio[int(s["s"] * SAMPLE_RATE): int(s["e"] * SAMPLE_RATE)]
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=chunk)
            stream.input_finished()
            if extractor.is_ready(stream):
                e = np.asarray(extractor.compute(stream), dtype=np.float32)
                embs.append(e / (np.linalg.norm(e) + 1e-9))
                used += s["e"] - s["s"]
        if embs:
            m = np.mean(embs, axis=0)
            out[spk] = (m / (np.linalg.norm(m) + 1e-9)).tolist()
    return out


def load_profiles() -> dict[str, dict]:
    if PROFILE_FILE.exists():
        try:
            return json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("Stimmprofile konnten nicht gelesen werden.")
    return {}


def save_profile(character_id: str, embedding: list[float]) -> None:
    """Merkt sich die Stimme einer Figur (gleitender Mittelwert über alle bestätigten Podcasts)."""
    profiles = load_profiles()
    new = np.asarray(embedding, dtype=np.float32)
    if character_id in profiles:
        old = np.asarray(profiles[character_id]["embedding"], dtype=np.float32)
        n = profiles[character_id].get("count", 1)
        new = (old * n + new) / (n + 1)
        new /= np.linalg.norm(new) + 1e-9
        profiles[character_id] = {"embedding": new.tolist(), "count": n + 1}
    else:
        profiles[character_id] = {"embedding": new.tolist(), "count": 1}
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_FILE.write_text(json.dumps(profiles), encoding="utf-8")


def match_profiles(embeddings: dict[int, list[float]], candidates: list[str]) -> dict[int, str]:
    """Ordnet erkannte Sprecher gespeicherten Stimmprofilen zu (beste Gesamtzuordnung)."""
    profiles = {k: v for k, v in load_profiles().items() if k in candidates}
    if not profiles or not embeddings:
        return {}
    sims = sorted(((float(np.dot(e, profiles[c]["embedding"])), s, c)
                   for s, e in embeddings.items() for c in profiles), reverse=True)
    result: dict[int, str] = {}
    for sim, s, c in sims:
        if sim >= MATCH_THRESHOLD and s not in result and c not in result.values():
            result[s] = c
    return result
