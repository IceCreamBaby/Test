"""Sprechererkennung (wer spricht wann?) + Stimmprofile zur automatischen Zuordnung.

Verfahren (getestet mit einer echten 79-minütigen Hobbylos-Folge):
1. Nur dort, wo die Spracherkennung Wörter gefunden hat, werden kurze Fenster (1,5 s) gebildet.
2. Jedes Fenster bekommt ein Stimm-Embedding (NeMo TitaNet über sherpa-onnx).
3. Die Fenster werden in die gewünschte Anzahl Stimmen gruppiert (k-Means auf der Einheitskugel).
   Fenster, die keiner Hauptstimme ähneln (z.B. eingefügte Werbung), gelten als "Sonstige".
4. Jedes Wort bekommt per Viterbi-Glättung einen Sprecher – Wechsel kosten etwas, damit einzelne
   unsichere Fenster nicht hin und her springen.
"""
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

EMBEDDING_REPO = "csukuangfj/speaker-embedding-models"
EMBEDDING_FILE = "nemo_en_titanet_small.onnx"
PROFILE_FILE = PROFILES_DIR / "profiles.json"
MATCH_THRESHOLD = 0.45

WIN = 1.5            # Fensterlänge in Sekunden
HOP = 0.5            # Fenster-Abstand
MIN_COVER = 0.5      # Anteil des Fensters, der Sprache enthalten muss
OTHER_SIM = 0.33     # darunter ähnelt ein Fenster keiner Hauptstimme -> "Sonstige"
TEMP = 9.0           # Schärfe der Sprecher-Wahrscheinlichkeiten
SWITCH_COST = 2.5    # Kosten eines Sprecherwechsels zwischen direkt aufeinanderfolgenden Wörtern
SWITCH_COST_PAUSE = 0.6  # ... nach einer Pause (> 0,5 s)
SAME_VOICE_SIM = 0.75    # Zentren ähnlicher als das: dieselbe Stimme (für die automatische Anzahl)


def _threads() -> int:
    return max(1, min(8, (os.cpu_count() or 4)))


def ensure_model(progress: ProgressFn | None = None) -> str:
    from huggingface_hub import hf_hub_download

    if progress:
        progress(0.0, "Lade Modell für die Sprechererkennung (einmalig) …")
    return hf_hub_download(EMBEDDING_REPO, EMBEDDING_FILE, cache_dir=str(MODELS_DIR / "diarization"))


def _extractor():
    import sherpa_onnx

    return sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=ensure_model(), num_threads=_threads()))


# --------------------------------------------------------------------------- Kernverfahren

def speech_windows(words: list[dict], duration: float) -> np.ndarray:
    """Startzeiten der Fenster, die überwiegend Sprache enthalten."""
    if not words:
        return np.zeros(0)
    res = 0.05
    cover = np.zeros(int(duration / res) + 2, dtype=bool)
    for w in words:
        cover[int(w["s"] / res):int(np.ceil(w["e"] / res))] = True
    cs = np.concatenate([[0], np.cumsum(cover)])
    n = int(WIN / res)
    starts = np.arange(0.0, max(0.0, duration - WIN) + 1e-9, HOP)
    idx = (starts / res).astype(int)
    frac = (cs[np.minimum(idx + n, len(cover))] - cs[idx]) / n
    return starts[frac >= MIN_COVER]


def window_embeddings(audio: np.ndarray, starts: np.ndarray, progress: ProgressFn | None = None) -> np.ndarray:
    ex = _extractor()
    out = []
    n = len(starts)
    for i, t in enumerate(starts):
        st = ex.create_stream()
        st.accept_waveform(sample_rate=SAMPLE_RATE,
                           waveform=audio[int(t * SAMPLE_RATE):int((t + WIN) * SAMPLE_RATE)])
        st.input_finished()
        e = np.asarray(ex.compute(st), dtype=np.float32)
        out.append(e / (np.linalg.norm(e) + 1e-9))
        if progress and i % 200 == 0:
            progress(0.05 + 0.75 * i / max(1, n), f"Analysiere Stimmen … {int(100 * i / max(1, n))} %")
    return np.stack(out) if out else np.zeros((0, 1), dtype=np.float32)


def _kmeans(E: np.ndarray, k: int, seed: int = 0, restarts: int = 6, iters: int = 40) -> tuple[np.ndarray, np.ndarray]:
    """Sphärisches k-Means mit k-means++-Start; ignoriert Ausreißer beim Neuberechnen der Zentren."""
    rng = np.random.default_rng(seed)
    best = None
    for _ in range(restarts):
        c = [E[rng.integers(len(E))]]
        for _ in range(1, k):
            d = 1 - np.max(E @ np.stack(c).T, axis=1)
            p = np.clip(d, 1e-9, None) ** 2
            c.append(E[rng.choice(len(E), p=p / p.sum())])
        C = np.stack(c)
        for _ in range(iters):
            S = E @ C.T
            lab = np.argmax(S, axis=1)
            keep = np.max(S, axis=1) >= OTHER_SIM
            newC = []
            for j in range(k):
                m = (lab == j) & keep
                v = E[m].mean(axis=0) if np.any(m) else C[j]
                newC.append(v / (np.linalg.norm(v) + 1e-9))
            newC = np.stack(newC)
            if np.allclose(newC, C, atol=1e-5):
                break
            C = newC
        S = E @ C.T
        score = float(np.sum(np.max(S, axis=1)))
        if best is None or score > best[0]:
            best = (score, np.argmax(S, axis=1), C)
    return best[1], best[2]


def _silhouette(E: np.ndarray, lab: np.ndarray, max_n: int = 1500) -> float:
    if len(set(lab.tolist())) < 2:
        return 0.0
    idx = np.random.default_rng(0).permutation(len(E))[:max_n]
    E, lab = E[idx], lab[idx]
    D = 1 - E @ E.T
    vals = []
    for i in range(len(E)):
        same = lab == lab[i]
        if same.sum() < 2:
            continue
        a = D[i, same].sum() / (same.sum() - 1)
        b = min(D[i, lab == j].mean() for j in set(lab.tolist()) if j != lab[i])
        vals.append((b - a) / max(a, b, 1e-9))
    return float(np.mean(vals)) if vals else 0.0


def choose_k(E: np.ndarray, max_k: int = 4) -> int:
    """Automatische Sprecheranzahl über die Silhouette."""
    if len(E) < 20:
        return 1
    best_k, best_s = 1, 0.12  # unter diesem Wert: nur eine Stimme
    for k in range(2, max_k + 1):
        lab, C = _kmeans(E, k)
        sim = C @ C.T
        if np.max(sim[~np.eye(k, dtype=bool)]) > SAME_VOICE_SIM:
            break  # zwei Zentren sind dieselbe Stimme -> mehr Sprecher gibt es nicht
        s = _silhouette(E, lab)
        if s > best_s + 0.01:
            best_k, best_s = k, s
    return best_k


def label_words(words: list[dict], starts: np.ndarray, S: np.ndarray) -> np.ndarray:
    """Viterbi über die Wörter. S: Ähnlichkeit jedes Fensters zu jedem Zentrum (Fenster × k).
    Zustände 0..k-1 = Hauptsprecher, k = Sonstige."""
    k = S.shape[1]
    n = len(words)
    emis = np.zeros((n, k + 1))
    if len(starts):
        for i, w in enumerate(words):
            mid = (w["s"] + w["e"]) / 2
            a = np.searchsorted(starts, mid - WIN, side="right")
            b = np.searchsorted(starts, mid, side="right")
            if b <= a:  # kein Fenster überdeckt das Wort: nächstes Fenster in der Nähe nehmen
                j = int(np.clip(np.searchsorted(starts, mid), 0, len(starts) - 1))
                if abs(starts[j] + WIN / 2 - mid) > 1.5:
                    continue
                a, b = j, j + 1
            # Fenster, deren Mitte näher am Wort liegt, zählen mehr
            centers = starts[a:b] + WIN / 2
            wgt = np.clip(1.0 - np.abs(centers - mid) / WIN, 0.05, None)
            sims = (S[a:b] * wgt[:, None]).sum(axis=0) / wgt.sum()
            emis[i, :k] = TEMP * sims
            emis[i, k] = TEMP * OTHER_SIM
    # Viterbi (Maximierung)
    score = emis[0].copy()
    back = np.zeros((n, k + 1), dtype=np.int32)
    for i in range(1, n):
        gap = words[i]["s"] - words[i - 1]["e"]
        sentence_end = words[i - 1]["w"].endswith((".", "?", "!", "…"))
        cost = SWITCH_COST_PAUSE if gap > 0.5 or sentence_end else SWITCH_COST
        trans = score[:, None] - cost * (1 - np.eye(k + 1))
        back[i] = np.argmax(trans, axis=0)
        score = trans[back[i], np.arange(k + 1)] + emis[i]
    path = np.zeros(n, dtype=np.int32)
    path[-1] = int(np.argmax(score))
    for i in range(n - 1, 0, -1):
        path[i - 1] = back[i, path[i]]
    return path


def diarize_words(audio: np.ndarray, words: list[dict], num_speakers: int = 2,
                  progress: ProgressFn | None = None) -> tuple[list[dict], dict[int, list[float]]]:
    """Sprecher-Segmente [{s, e, spk(, other)}] und Stimmprofile {spk: embedding}."""
    duration = len(audio) / SAMPLE_RATE
    if not words:
        return [], {}
    if progress:
        progress(0.0, "Erkenne Sprecher …")
    starts = speech_windows(words, duration)
    if len(starts) < 4 or num_speakers == 1:
        return [{"s": 0.0, "e": duration, "spk": 0}], {}
    E = window_embeddings(audio, starts, progress)
    if progress:
        progress(0.82, "Gruppiere Stimmen …")
    k = num_speakers if num_speakers and num_speakers > 0 else choose_k(E)
    if k <= 1:
        return [{"s": 0.0, "e": duration, "spk": 0}], {0: _norm(E.mean(axis=0)).tolist()}
    _, C = _kmeans(E, k)
    S = E @ C.T
    if progress:
        progress(0.9, "Ordne die Wörter den Sprechern zu …")
    path = label_words(words, starts, S)
    # Hauptsprecher nach Redezeit nummerieren, "Sonstige" (= k) ans Ende
    talk = np.zeros(k + 1)
    for w, lab in zip(words, path):
        talk[lab] += w["e"] - w["s"]
    order = sorted(range(k), key=lambda j: -talk[j])
    used_main = [j for j in order if talk[j] > 0]
    remap = {old: new for new, old in enumerate(used_main)}
    other_id = len(used_main)
    remap[k] = other_id
    segs: list[dict] = []
    for w, lab in zip(words, path):
        spk = remap.get(int(lab), other_id)
        if segs and segs[-1]["spk"] == spk and w["s"] - segs[-1]["e"] <= 0.8:
            segs[-1]["e"] = round(w["e"], 3)
        else:
            seg = {"s": round(w["s"], 3), "e": round(w["e"], 3), "spk": spk}
            if spk == other_id:
                seg["other"] = True
            segs.append(seg)
    embs = {remap[j]: C[j].tolist() for j in used_main}
    return segs, embs


def _norm(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) + 1e-9)


# --------------------------------------------------------------------------- eigener Prozess

def diarize_isolated(wav_path, num_speakers: int = 2, words: list[dict] | None = None,
                     progress: ProgressFn | None = None,
                     cancel: threading.Event | None = None) -> tuple[list[dict], dict[int, list[float]]]:
    """Sprechererkennung in einem eigenen Prozess (rechenintensiv; die Oberfläche bleibt bedienbar
    und der Vorgang lässt sich sauber abbrechen)."""
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=_isolated_worker, args=(str(wav_path), num_speakers, words or [], q), daemon=True)
    proc.start()
    try:
        while True:
            try:
                msg = q.get(timeout=0.5)
            except queue.Empty:
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("Abgebrochen") from None
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


def _isolated_worker(wav_path: str, num_speakers: int, words: list[dict], q) -> None:
    try:
        from .media import read_wav

        audio, _ = read_wav(wav_path)
        segs, embs = diarize_words(audio, words, num_speakers,
                                   progress=lambda f, m: q.put(("progress", f, m)))
        q.put(("result", segs, embs))
    except Exception as exc:
        q.put(("error", f"{type(exc).__name__}: {exc}"))


# --------------------------------------------------------------------------- Stimmprofile

def load_profiles() -> dict[str, dict]:
    if PROFILE_FILE.exists():
        try:
            data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
            # Profile eines anderen Stimm-Modells sind nicht vergleichbar
            return {k: v for k, v in data.items() if v.get("model") == EMBEDDING_FILE}
        except Exception:
            log.warning("Stimmprofile konnten nicht gelesen werden.")
    return {}


def save_profile(character_id: str, embedding: list[float]) -> None:
    """Merkt sich die Stimme einer Figur (gleitender Mittelwert über alle bestätigten Podcasts)."""
    profiles = load_profiles()
    new = np.asarray(embedding, dtype=np.float32)
    old = profiles.get(character_id)
    if old and len(old["embedding"]) == len(new):
        n = old.get("count", 1)
        new = (np.asarray(old["embedding"], dtype=np.float32) * n + new) / (n + 1)
        count = n + 1
    else:
        count = 1
    new /= np.linalg.norm(new) + 1e-9
    profiles[character_id] = {"embedding": new.tolist(), "count": count, "model": EMBEDDING_FILE}
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_FILE.write_text(json.dumps(profiles), encoding="utf-8")


def match_profiles(embeddings: dict[int, list[float]], candidates: list[str]) -> dict[int, str]:
    """Ordnet erkannte Sprecher gespeicherten Stimmprofilen zu (beste Paare zuerst)."""
    profiles = {k: v for k, v in load_profiles().items() if k in candidates}
    if not profiles or not embeddings:
        return {}
    sims = sorted(((float(np.dot(e, profiles[c]["embedding"])), s, c)
                   for s, e in embeddings.items() for c in profiles
                   if len(profiles[c]["embedding"]) == len(e)), reverse=True)
    result: dict[int, str] = {}
    for sim, s, c in sims:
        if sim >= MATCH_THRESHOLD and s not in result and c not in result.values():
            result[s] = c
    return result
