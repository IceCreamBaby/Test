"""Sprechererkennung (wer spricht wann?) mit sherpa-onnx + Stimmprofile zur automatischen Zuordnung."""
from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import queue
import threading
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


FINE_THRESHOLD = 0.5     # sherpa: feine Vor-Aufteilung in (eher zu viele) Stimmgruppen
SEED_MIN_SHARE = 0.03    # Hauptsprecher brauchen mind. 3 % der Redezeit
SEED_MAX_SIM = 0.55      # Hauptsprecher müssen klar verschiedene Stimmen haben
ASSIGN_MIN_SIM = 0.35    # darunter: fremde Stimme (Werbung, Einspieler) -> "Sonstige"


def diarize(audio: np.ndarray, num_speakers: int = 2, progress: ProgressFn | None = None) -> list[dict]:
    """Gibt eine nach Startzeit sortierte Liste [{s, e, spk}] zurück.

    Ablauf: sherpa-onnx teilt die Aufnahme fein in Stimmgruppen auf; danach werden die größten, klar
    unterscheidbaren Gruppen als Hauptsprecher gewählt und alle übrigen Gruppen per Stimmähnlichkeit
    zugeordnet. Fremde Stimmen (z.B. eingefügte Werbung) landen als eigener Sprecher am Ende der
    Liste, statt einen Hauptsprecher-Platz zu belegen.
    """
    import sherpa_onnx

    seg_model, emb_model = ensure_models(progress)
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=seg_model),
            num_threads=_threads(),
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb_model, num_threads=_threads()),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=FINE_THRESHOLD),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise RuntimeError("Konfiguration der Sprechererkennung ist ungültig.")
    sd = sherpa_onnx.OfflineSpeakerDiarization(config)

    def cb(done: int, total: int) -> int:
        if progress and total:
            progress(0.9 * done / total, f"Erkenne Sprecher … {int(100 * done / total)} %")
        return 0

    if progress:
        progress(0.0, "Erkenne Sprecher …")
    result = sd.process(np.ascontiguousarray(audio, dtype=np.float32), callback=cb).sort_by_start_time()
    segs = [{"s": round(float(r.start), 3), "e": round(float(r.end), 3), "spk": int(r.speaker)} for r in result]
    if progress:
        progress(0.92, "Fasse Stimmgruppen zusammen …")
    embs = speaker_embeddings(audio, segs, max_seconds=30)
    return merge_clusters(segs, embs, num_speakers)


def merge_clusters(segs: list[dict], embs: dict[int, list[float]], num_speakers: int | None) -> list[dict]:
    """Fasst feine Stimmgruppen zu Hauptsprechern zusammen (num_speakers <= 0: automatisch).

    Ergebnis: Sprecher 0..k-1 = Hauptsprecher (nach Redezeit sortiert), Sprecher k = Sonstige (falls nötig).
    """
    if not segs:
        return segs
    talk: dict[int, float] = {}
    for sg in segs:
        talk[sg["spk"]] = talk.get(sg["spk"], 0.0) + sg["e"] - sg["s"]
    total = sum(talk.values()) or 1.0
    vec = {k: np.asarray(v, dtype=np.float32) for k, v in embs.items()}
    order = sorted(talk, key=lambda k: -talk[k])
    limit = num_speakers if num_speakers and num_speakers > 0 else 99
    seeds: list[int] = []
    for k in order:
        if len(seeds) >= limit:
            break
        if k not in vec or talk[k] < SEED_MIN_SHARE * total:
            continue
        if all(float(np.dot(vec[k], vec[s])) < SEED_MAX_SIM for s in seeds):
            seeds.append(k)
    for k in order:  # notfalls auffüllen, damit es genug Hauptsprecher gibt
        if len(seeds) >= min(limit, len(order)) or limit == 99:
            break
        if k not in seeds:
            seeds.append(k)
    if not seeds:
        seeds = [order[0]]
    label: dict[int, int] = {k: i for i, k in enumerate(seeds)}
    other = len(seeds)
    for k in order:
        if k in label:
            continue
        if k in vec:
            sims = [float(np.dot(vec[k], vec[s])) if s in vec else -1.0 for s in seeds]
            best = int(np.argmax(sims))
            label[k] = best if sims[best] >= ASSIGN_MIN_SIM else other
        else:
            label[k] = other  # zu kurz für ein Stimmprofil
    out = [dict(sg, spk=label[sg["spk"]]) for sg in segs]
    # Hauptsprecher nach Redezeit nummerieren, "Sonstige" bleibt am Ende
    main_talk = {i: 0.0 for i in range(other)}
    for sg in out:
        if sg["spk"] < other:
            main_talk[sg["spk"]] += sg["e"] - sg["s"]
    ranking = {old: new for new, old in enumerate(sorted(main_talk, key=lambda i: -main_talk[i]))}
    ranking[other] = other
    for sg in out:
        sg["spk"] = ranking[sg["spk"]]
        if sg["spk"] == other:
            sg["other"] = True
    return _merge_adjacent(out)


def _merge_adjacent(segs: list[dict], gap: float = 0.3) -> list[dict]:
    out: list[dict] = []
    for sg in sorted(segs, key=lambda x: x["s"]):
        if out and out[-1]["spk"] == sg["spk"] and sg["s"] - out[-1]["e"] <= gap:
            out[-1]["e"] = max(out[-1]["e"], sg["e"])
        else:
            out.append(dict(sg))
    return out


def diarize_isolated(wav_path, num_speakers: int = 2, progress: ProgressFn | None = None,
                     cancel: threading.Event | None = None) -> tuple[list[dict], dict[int, list[float]]]:
    """Sprechererkennung + Stimmprofile in einem eigenen Prozess.

    sherpa-onnx gibt den Python-GIL während der Berechnung nicht frei – im Hauptprozess würde die
    Oberfläche sonst minutenlang hängen. Außerdem lässt sich ein eigener Prozess sauber abbrechen.
    """
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=_isolated_worker, args=(str(wav_path), num_speakers, q), daemon=True)
    proc.start()
    try:
        while True:
            try:
                msg = q.get(timeout=0.5)
            except queue.Empty:
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("Abgebrochen")
                if not proc.is_alive():
                    try:
                        msg = q.get(timeout=2)
                    except queue.Empty:
                        raise RuntimeError(f"Sprechererkennung unerwartet beendet (Code {proc.exitcode}).") from None
                else:
                    continue
            if msg[0] == "progress":
                if progress:
                    progress(msg[1], msg[2])
            elif msg[0] == "result":
                return msg[1], {int(k): v for k, v in msg[2].items()}
            else:
                raise RuntimeError(f"Sprechererkennung fehlgeschlagen: {msg[1]}")
    finally:
        if proc.is_alive():
            proc.terminate()
        proc.join(timeout=10)


def _isolated_worker(wav_path: str, num_speakers: int, q) -> None:
    try:
        from .media import read_wav

        audio, _ = read_wav(wav_path)
        segs = diarize(audio, num_speakers, progress=lambda f, m: q.put(("progress", f, m)))
        q.put(("progress", 1.0, "Berechne Stimmprofile …"))
        try:
            embs = speaker_embeddings(audio, segs)
        except Exception as exc:  # Stimmprofile sind optional
            log.warning("Stimmprofile nicht berechnet: %s", exc)
            embs = {}
        q.put(("result", segs, embs))
    except Exception as exc:
        q.put(("error", f"{type(exc).__name__}: {exc}"))


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
