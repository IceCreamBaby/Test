"""Berechnet für jeden Frame den Zustand jeder Figur (Lippen, Blinzeln, Blick, Gesten, Lachen …)."""
from __future__ import annotations

import math
import random
import unicodedata

import numpy as np

from ..transcript import is_laugh
from .characters import CharState

VOWEL_A = set("aäáà")
VOWEL_O = set("oöuüóòúù")
VOWEL_E = set("eiyéèíì")
LIPS = set("mbp")
TEETH = set("fvw")


def viseme_for_char(ch: str) -> str:
    ch = ch.lower()
    if ch in VOWEL_A:
        return "A"
    if ch in VOWEL_O:
        return "O"
    if ch in VOWEL_E:
        return "E"
    if ch in LIPS:
        return "M"
    if ch in TEETH:
        return "F"
    return "C"


def _letters(word: str) -> str:
    w = "".join(ch for ch in unicodedata.normalize("NFC", word) if ch.isalpha())
    return w or "a"


def _smooth(x: np.ndarray, attack: float, release: float) -> np.ndarray:
    y = np.zeros_like(x)
    v = 0.0
    for i, target in enumerate(x):
        k = attack if target > v else release
        v += (target - v) * k
        y[i] = v
    return y


def _pulse_track(n: int, fps: float, events: list[tuple[float, float]], attack: float = 0.25,
                 release: float = 0.3) -> np.ndarray:
    """0..1-Spur aus (start, ende)-Ereignissen mit weichem Ein- und Ausblenden."""
    raw = np.zeros(n, dtype=np.float32)
    for s, e in events:
        a, b = int(s * fps), int(math.ceil(e * fps))
        raw[max(0, a):max(0, min(n, b))] = 1.0
    return _smooth(raw, 1 - math.exp(-1 / (attack * fps)), 1 - math.exp(-1 / (release * fps)))


class Animator:
    """Erzeugt CharState-Objekte für alle Figuren und Frames."""

    def __init__(self, words: list[dict], env: np.ndarray, fps: float, n_frames: int, slot_of_spk: dict[int, int],
                 slots_inner: list[int], liveliness: float = 1.0, seed: int = 3):
        self.fps = fps
        self.n = n_frames
        self.words = words
        self.env = np.pad(env, (0, max(0, n_frames - len(env))))[:n_frames]
        self.slot_of_spk = slot_of_spk
        self.n_slots = len(slots_inner)
        self.inner = slots_inner
        self.live = liveliness
        self.rng = random.Random(seed)
        self._build()

    # ---------------------------------------------------------------- Vorberechnung
    def _build(self) -> None:
        n, fps = self.n, self.fps
        S = self.n_slots
        active = np.zeros((S, n), dtype=np.float32)
        self.word_at: list[list[int]] = [[-1] * n for _ in range(S)]
        laugh_events: list[list[tuple[float, float]]] = [[] for _ in range(S)]
        question_events: list[list[tuple[float, float]]] = [[] for _ in range(S)]
        by_slot: list[list[dict]] = [[] for _ in range(S)]
        for idx, w in enumerate(self.words):
            slot = self.slot_of_spk.get(w.get("spk", 0), 0)
            if slot >= S:
                continue
            by_slot[slot].append(w)
            a = int((w["s"] - 0.04) * fps)
            b = int(math.ceil((w["e"] + 0.06) * fps))
            active[slot, max(0, a):max(0, min(n, b))] = 1.0
            for f in range(max(0, int(w["s"] * fps)), min(n, int(math.ceil(w["e"] * fps)))):
                self.word_at[slot][f] = idx
            if is_laugh(w["w"]):
                laugh_events[slot].append((w["s"] - 0.1, w["e"] + 0.5))
            if w["w"].endswith("?"):
                question_events[slot].append((w["s"] - 0.3, w["e"] + 0.5))
        # kleine Lücken zwischen Wörtern desselben Sprechers schließen
        for slot, ws in enumerate(by_slot):
            for w1, w2 in zip(ws, ws[1:]):
                if 0 < w2["s"] - w1["e"] < 0.3:
                    active[slot, int(w1["e"] * fps):int(math.ceil(w2["s"] * fps))] = 1.0
        self.active = active
        env = self.env
        opening = np.clip((env - 0.06) / 0.75, 0.0, 1.0) ** 0.75
        self.talk = np.stack([_smooth(opening * active[s], 0.65, 0.4) for s in range(S)]) if S else np.zeros((0, n))
        # wer spricht gerade (für Blicke)
        loud = np.stack([_smooth(active[s], 0.3, 0.05) for s in range(S)]) if S else np.zeros((0, n))
        self.speaking = loud > 0.5
        self.current_speaker = np.full(n, -1, dtype=np.int32)
        last = -1
        for f in range(n):
            vals = loud[:, f] if S else []
            if S and vals.max() > 0.3:
                last = int(vals.argmax())
            self.current_speaker[f] = last
        self.laugh = np.stack([_pulse_track(n, fps, laugh_events[s], 0.12, 0.5) for s in range(S)])
        # Zuhörer lacht manchmal mit
        for s in range(S):
            others = [e for o in range(S) if o != s for e in laugh_events[o]]
            shared = [(a + 0.15, b) for a, b in others if self.rng.random() < 0.6]
            self.laugh[s] = np.maximum(self.laugh[s], _pulse_track(n, fps, shared, 0.2, 0.6) * 0.8)
        self.brow_q = np.stack([_pulse_track(n, fps, question_events[s], 0.15, 0.4) for s in range(S)])
        # Betonung -> Augenbrauen hoch
        self.brow_emph = np.zeros((S, n), dtype=np.float32)
        for s in range(S):
            ev = []
            last_f = -10 ** 9
            for f in range(1, n):
                if self.talk[s, f] > 0.85 and env[f] > 1.0 and f - last_f > fps * 2.2:
                    ev.append((f / fps, f / fps + 0.45))
                    last_f = f
            self.brow_emph[s] = _pulse_track(n, fps, ev, 0.08, 0.35)
        self.blink = np.stack([self._blinks() for _ in range(S)])
        self.gesture = [self._gestures(s) for s in range(S)]
        self.nods = np.stack([self._nods(s) for s in range(S)])
        self.gaze = [self._gaze(s) for s in range(S)]
        self.phase = [self.rng.uniform(0, math.tau) for _ in range(S)]

    def _blinks(self) -> np.ndarray:
        n, fps = self.n, self.fps
        out = np.zeros(n, dtype=np.float32)
        t = self.rng.uniform(0.5, 2.5)
        dur = 0.16
        while t < n / fps:
            a = int(t * fps)
            for f in range(a, min(n, a + int(dur * fps) + 1)):
                p = (f / fps - t) / dur
                out[f] = max(out[f], 1.0 - abs(2 * p - 1))
            # manchmal Doppel-Blinzeln
            t += 0.3 if self.rng.random() < 0.12 else self.rng.uniform(2.0, 5.5)
        return out

    def _gestures(self, s: int) -> tuple[np.ndarray, np.ndarray]:
        n, fps = self.n, self.fps
        left_ev, right_ev = [], []
        f = 0
        side = self.rng.random() < 0.5
        cooldown = 0
        while f < n:
            if self.speaking[s, f] and cooldown <= 0:
                # wie lange spricht die Figur noch?
                g = f
                while g < n and self.speaking[s, g]:
                    g += 1
                remaining = (g - f) / fps
                if remaining > 1.4 and self.rng.random() < 0.55 * self.live:
                    dur = min(remaining - 0.2, self.rng.uniform(1.2, 2.6))
                    (left_ev if side else right_ev).append((f / fps, f / fps + dur))
                    if self.rng.random() < 0.25:  # beide Hände
                        (right_ev if side else left_ev).append((f / fps + 0.2, f / fps + dur))
                    side = not side
                    cooldown = int((dur + self.rng.uniform(1.5, 4.0)) * fps)
                else:
                    cooldown = int(fps * 0.8)
            f += 1
            cooldown -= 1
        return (_pulse_track(n, fps, left_ev, 0.22, 0.3), _pulse_track(n, fps, right_ev, 0.22, 0.3))

    def _nods(self, s: int) -> np.ndarray:
        n, fps = self.n, self.fps
        out = np.zeros(n, dtype=np.float32)
        t = self.rng.uniform(0.5, 2.0)
        while t < n / fps:
            f = int(t * fps)
            if f < n and not self.speaking[s, f] and self.current_speaker[f] >= 0:
                dur = 0.55
                for k in range(int(dur * fps)):
                    if f + k < n:
                        out[f + k] = math.sin(math.pi * k / (dur * fps))
            t += self.rng.uniform(1.8, 4.5) / max(0.3, self.live)
        return out

    def _gaze(self, s: int) -> np.ndarray:
        """Blickrichtung x (-1..1): Zuhörer schaut zum Sprecher, Sprecher mal zum Partner, mal in die Kamera."""
        n, fps = self.n, self.fps
        target = np.zeros(n, dtype=np.float32)
        mode_until, mode = 0, "partner"
        for f in range(n):
            if f >= mode_until:
                mode = "camera" if self.rng.random() < 0.35 else "partner"
                mode_until = f + int(self.rng.uniform(1.2, 3.5) * fps)
            spk = self.current_speaker[f]
            if self.speaking[s, f]:
                target[f] = 0.0 if mode == "camera" else self.inner[s] * 0.8
            elif spk >= 0 and spk != s:
                target[f] = self.inner[s] * 0.9 if self.n_slots > 1 else 0.0
            else:
                target[f] = self.inner[s] * 0.4 if mode == "partner" else 0.0
        return _smooth(target, 0.25, 0.25)

    # ---------------------------------------------------------------- Zustand pro Frame
    def state(self, s: int, f: int) -> CharState:
        t = f / self.fps
        ph = self.phase[s]
        talk = float(self.talk[s, f])
        speaking = bool(self.speaking[s, f])
        laugh = float(self.laugh[s, f])
        nod = float(self.nods[s, f])
        gaze = float(self.gaze[s][f])
        live = self.live
        viseme = "rest"
        wi = self.word_at[s][f]
        if wi >= 0 and talk > 0.05:
            w = self.words[wi]
            letters = _letters(w["w"])
            p = (t - w["s"]) / max(0.05, w["e"] - w["s"])
            viseme = viseme_for_char(letters[min(len(letters) - 1, max(0, int(p * len(letters))))])
        head_dy = (-7 * talk + 2.0 * math.sin(t * 2.3 + ph)) * live + 9 * nod
        head_rot = (math.sin(t * 1.25 + ph) * (2.6 if speaking else 1.2) + gaze * 3.5) * live
        if laugh > 0.2:
            head_rot += math.sin(t * 22) * 2.0 * laugh
            head_dy += -4 * laugh + math.sin(t * 26) * 2 * laugh
        gl, gr = self.gesture[s]
        return CharState(
            talk=talk if laugh < 0.3 else max(talk, 0.4 + 0.3 * abs(math.sin(t * 12))),
            viseme=viseme,
            blink=float(self.blink[s, f]),
            gaze_x=gaze,
            gaze_y=0.15 * nod,
            head_dx=gaze * 4 * live,
            head_dy=head_dy,
            head_rot=head_rot,
            body_dy=2.5 * math.sin(t * math.tau / 4.2 + ph) + (1.5 * math.sin(t * 24) * laugh),
            brow=float(min(1.0, self.brow_q[s, f] + self.brow_emph[s, f] + 0.4 * laugh)),
            laugh=laugh,
            smile=0.3 + 0.5 * laugh,
            facing=gaze * 0.6,
            gesture_l=float(gl[f]),
            gesture_r=float(gr[f]),
            gesture_phase=t * 3.0 + ph,
            speaking=speaking,
        )
