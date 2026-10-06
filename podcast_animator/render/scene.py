"""Studio-Hintergrund, Tisch, Mikrofone, Figuren-Positionen und Kamera-Planung."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

import numpy as np
import skia

from .draw import (blob, blurred, color, fill, font, linear, oval, path_from, radial, rrect, shade, smooth_closed,
                   stroke)

W, H = 1080, 1920          # Ausgabeformat (YouTube Shorts 9:16)
DESK_Y = 1290.0            # Oberkante des Tisches in Weltkoordinaten
LAYER_SCALE = 2.0          # statische Ebenen werden doppelt so groß gerastert (scharf beim Zoomen)

THEMES = {
    "lila": {"wall_top": "#2d2150", "wall_bottom": "#120d24", "panel": "#3b2c66", "accent": "#ff4fd8",
             "accent2": "#3ee0ff", "desk": "#241b2e", "desk_top": "#3e3150", "shelf": "#4a3a2c"},
    "nacht": {"wall_top": "#16233b", "wall_bottom": "#080d18", "panel": "#1f3150", "accent": "#3ee0ff",
              "accent2": "#ffb020", "desk": "#141a24", "desk_top": "#2a3444", "shelf": "#3b2f26"},
    "warm": {"wall_top": "#5b3a2e", "wall_bottom": "#2a1813", "panel": "#6d4636", "accent": "#ffb347",
             "accent2": "#ff5f6d", "desk": "#3a2418", "desk_top": "#6b4630", "shelf": "#2e1d14"},
    "gruen": {"wall_top": "#1f3d33", "wall_bottom": "#0c1a15", "panel": "#2a5144", "accent": "#7dff8a",
              "accent2": "#ffe14d", "desk": "#16231e", "desk_top": "#2f463c", "shelf": "#4a3a2c"},
}


@dataclass
class Slot:
    """Platz einer Figur im Studio."""
    x: float
    scale: float
    inner: int  # +1 = Bildmitte liegt rechts, -1 = links

    @property
    def head(self) -> tuple[float, float]:
        return self.x, DESK_Y - 420 * self.scale


def make_slots(n: int) -> list[Slot]:
    if n <= 1:
        return [Slot(540, 1.1, 1)]
    if n == 2:
        return [Slot(282, 1.0, 1), Slot(798, 1.0, -1)]
    if n == 3:
        return [Slot(188, 0.78, 1), Slot(540, 0.78, -1), Slot(892, 0.78, -1)]
    step = W / n
    sc = min(0.74, 2.6 / n)
    return [Slot(step * (i + 0.5), sc, 1 if step * (i + 0.5) < W / 2 else -1) for i in range(n)]


# --------------------------------------------------------------------------- statische Ebenen

class Layer:
    """Statische Ebene, vorgerastert in zwei Auflösungen und auf ihren Inhalt zugeschnitten."""

    LINEAR = skia.SamplingOptions(skia.FilterMode.kLinear)

    def __init__(self, draw_fn, rect: skia.Rect):
        self.rect = rect
        self.images = {k: self._raster(draw_fn, rect, k) for k in (1.0, LAYER_SCALE)}

    @staticmethod
    def _raster(draw_fn, rect: skia.Rect, k: float) -> skia.Image:
        surf = skia.Surface(max(1, int(math.ceil(rect.width() * k))), max(1, int(math.ceil(rect.height() * k))))
        c = surf.getCanvas()
        c.clear(skia.ColorTRANSPARENT)
        c.scale(k, k)
        c.translate(-rect.left(), -rect.top())
        draw_fn(c)
        return surf.makeImageSnapshot()

    def draw(self, c: skia.Canvas, zoom: float) -> None:
        img = self.images[1.0 if zoom < 1.3 else LAYER_SCALE]
        c.drawImageRect(img, self.rect, self.LINEAR)


def build_layers(theme_name: str, slots: list[Slot], sign_text: str, seed: int = 7,
                 with_mic: list[bool] | None = None) -> dict[str, list[Layer]]:
    th = THEMES.get(theme_name, THEMES["lila"])
    rng = random.Random(seed)
    mics = []
    for i, sl in enumerate(slots):
        if with_mic is not None and not with_mic[i]:
            continue
        k = sl.scale
        cx = sl.x + sl.inner * 75 * k
        mics.append(Layer(lambda c, sl=sl: _draw_mic(c, sl),
                          skia.Rect.MakeLTRB(cx - 125 * k, DESK_Y - 335 * k, cx + 125 * k, DESK_Y + 80 * k)))
    return {
        "bg": [Layer(lambda c: _draw_background(c, th, slots, sign_text, rng), skia.Rect.MakeWH(W, H))],
        "desk": [Layer(lambda c: _draw_desk(c, th), skia.Rect.MakeLTRB(0, DESK_Y - 40, W, H))],
        "mics": mics,
    }


def _draw_background(c: skia.Canvas, th: dict, slots: list[Slot], sign_text: str, rng: random.Random) -> None:
    # Wand mit Verlauf
    c.drawRect(skia.Rect.MakeWH(W, H), linear(fill("#000000"), (0, 0), (0, DESK_Y + 200),
                                               [th["wall_top"], th["wall_bottom"]]))
    # dezente vertikale Holzlatten
    for i in range(0, W, 54):
        c.drawRect(skia.Rect.MakeXYWH(i, 0, 3, DESK_Y), fill("#000000", 0.12))
    # Akustik-Paneele hinter den Figuren
    for s in slots:
        pw, ph = 340 * s.scale, 400 * s.scale
        x0, y0 = s.x - pw / 2, DESK_Y - 640 * s.scale - 40
        c.drawPath(rrect(x0 - 10, y0 - 10, pw + 20, ph + 20, 18), fill("#000000", 0.25))
        cols, rows = 4, 5
        cw, rh = pw / cols, ph / rows
        for r in range(rows):
            for k in range(cols):
                px, py = x0 + k * cw + 4, y0 + r * rh + 4
                tile = rrect(px, py, cw - 8, rh - 8, 10)
                c.drawPath(tile, linear(fill("#000000"), (px, py), (px + cw, py + rh),
                                        [shade(th["panel"], 1.12), shade(th["panel"], 0.8)]))
                # Pyramiden-Schaumstoff andeuten
                c.drawPath(path_from([("M", px + 6, py + rh - 14), ("L", px + cw / 2 - 4, py + 8),
                                      ("L", px + cw - 14, py + rh - 14)], closed=False),
                           stroke(shade(th["panel"], 0.7), 2.5, alpha=0.7))
    # Regale an den Seiten
    for side in (-1, 1):
        for y in (430, 760):
            sx0 = 0 if side < 0 else W - 150
            c.drawPath(rrect(sx0 - 10, y, 170, 18, 6), fill(th["shelf"]))
            c.drawRect(skia.Rect.MakeXYWH(sx0 - 10, y + 18, 170, 8), fill("#000000", 0.3))
            _shelf_items(c, sx0, y, side, th, rng)
    # Neon-Schild zwischen den Köpfen
    if sign_text:
        _neon(c, sign_text, W / 2, 500, th["accent"])
    # kleine Lichterkette / Spots
    for i in range(9):
        x = 60 + i * 120
        y = 70 + 14 * math.sin(i * 1.3)
        c.drawCircle(x, y, 26, blurred(fill(th["accent2"], 0.35), 10))
        c.drawCircle(x, y, 7, fill("#fff7d6"))
    c.drawPath(path_from([("M", 0, 60)] + [("Q", 60 + i * 120 - 60, 100, 60 + i * 120, 70 + 14 * math.sin(i * 1.3))
                                            for i in range(9)] + [("Q", 1050, 100, W, 60)], closed=False),
               stroke("#000000", 3, alpha=0.5))
    # Licht-Kegel
    for s in slots:
        c.drawCircle(s.x, DESK_Y - 430 * s.scale, 380 * s.scale,
                     radial(skia.Paint(AntiAlias=True), (s.x, DESK_Y - 430 * s.scale), 380 * s.scale,
                            [color("#ffffff", 0.10), color("#ffffff", 0.0)]))
    # Vignette
    c.drawRect(skia.Rect.MakeWH(W, H), radial(skia.Paint(AntiAlias=True), (W / 2, H * 0.45), H * 0.75,
                                              [color("#000000", 0.0), color("#000000", 0.45)], [0.55, 1.0]))


def _shelf_items(c: skia.Canvas, x0: float, y: float, side: int, th: dict, rng: random.Random) -> None:
    x = x0 + 8
    kinds = ["books", "plant", "controller", "books", "cube"]
    rng.shuffle(kinds)
    for kind in kinds[:2]:
        if kind == "books":
            for _ in range(rng.randint(3, 5)):
                bw, bh = rng.randint(14, 22), rng.randint(56, 84)
                col = rng.choice(["#e45858", "#f2b84b", "#5aa9e6", "#8bd17c", "#c084fc", "#f2f2f2"])
                c.drawPath(rrect(x, y - bh, bw, bh, 3), fill(shade(col, 0.85)))
                c.drawRect(skia.Rect.MakeXYWH(x + 3, y - bh + 10, bw - 6, 5), fill("#ffffff", 0.35))
                x += bw + 2
            x += 10
        elif kind == "plant":
            px = x + 30
            for k in range(7):
                ang = -math.pi / 2 + (k - 3) * 0.38
                lx, ly = px + math.cos(ang) * 46, y - 40 + math.sin(ang) * 46
                leaf = smooth_closed([(px, y - 36), ((px + lx) / 2 - 8, (y - 36 + ly) / 2), (lx, ly),
                                      ((px + lx) / 2 + 8, (y - 36 + ly) / 2)], 0.8)
                c.drawPath(leaf, fill("#3fae6a" if k % 2 else "#2e8c55"))
            c.drawPath(path_from([("M", px - 22, y - 40), ("L", px + 22, y - 40), ("L", px + 16, y), ("L", px - 16, y)]),
                       fill("#d9734e"))
            x += 70
        elif kind == "controller":
            cx = x + 40
            body = blob([(cx - 40, y - 30), (cx, y - 36), (cx + 40, y - 30), (cx + 44, y - 4), (cx + 30, y),
                         (cx, y - 10), (cx - 30, y), (cx - 44, y - 4)])
            c.drawPath(body, fill("#2c2c34"))
            c.drawCircle(cx + 20, y - 22, 4, fill("#ff5f6d"))
            c.drawCircle(cx + 28, y - 14, 4, fill("#5aa9e6"))
            c.drawRect(skia.Rect.MakeXYWH(cx - 30, y - 22, 16, 5), fill("#9a9aa8"))
            c.drawRect(skia.Rect.MakeXYWH(cx - 24.5, y - 27.5, 5, 16), fill("#9a9aa8"))
            x += 90
        else:
            c.drawPath(rrect(x + 6, y - 46, 46, 46, 8), fill(th["accent2"]))
            c.drawPath(rrect(x + 14, y - 38, 30, 30, 5), fill("#ffffff", 0.25))
            x += 64


def _neon(c: skia.Canvas, text: str, cx: float, cy: float, col: str) -> None:
    text = text.upper()[:14]
    size = 74 if len(text) <= 8 else max(40, 74 * 8 / len(text))
    f = font("LuckiestGuy-Regular.ttf", size)
    w = f.measureText(text)
    x = cx - w / 2
    pad = 34
    c.drawPath(rrect(x - pad, cy - size - 6, w + pad * 2, size + 44, 22), fill("#000000", 0.35))
    c.drawPath(rrect(x - pad, cy - size - 6, w + pad * 2, size + 44, 22), stroke(col, 4, alpha=0.6))
    glow = blurred(fill(col, 0.9), 18)
    c.drawString(text, x, cy + 8, f, glow)
    c.drawString(text, x, cy + 8, f, blurred(fill(col, 1.0), 5))
    c.drawString(text, x, cy + 8, f, fill(shade(col, 1.55)))


def _draw_desk(c: skia.Canvas, th: dict) -> None:
    front_y = DESK_Y + 92
    # Schatten der Tischplatte auf die Wand / Figuren
    c.drawRect(skia.Rect.MakeLTRB(0, DESK_Y - 26, W, DESK_Y + 4), blurred(fill("#000000", 0.35), 10))
    top = path_from([("M", -40, DESK_Y), ("L", W + 40, DESK_Y), ("L", W + 80, front_y), ("L", -80, front_y)])
    c.drawPath(top, linear(fill("#000000"), (0, DESK_Y), (0, front_y),
                           [shade(th["desk_top"], 1.1), shade(th["desk_top"], 0.85)]))
    # Maserung
    for i in range(6):
        y = DESK_Y + 12 + i * 14
        c.drawPath(path_from([("M", -40, y), ("Q", W / 2, y + (6 if i % 2 else -6), W + 40, y)], closed=False),
                   stroke("#000000", 2, alpha=0.08))
    c.drawRect(skia.Rect.MakeLTRB(0, front_y, W, front_y + 14), fill(shade(th["desk_top"], 1.3)))
    # Front mit LED-Leiste
    c.drawRect(skia.Rect.MakeLTRB(0, front_y + 14, W, H), linear(fill("#000000"), (0, front_y), (0, H),
                                                              [th["desk"], shade(th["desk"], 0.55)]))
    c.drawRect(skia.Rect.MakeLTRB(0, front_y + 26, W, front_y + 36), blurred(fill(th["accent"], 0.9), 12))
    c.drawRect(skia.Rect.MakeLTRB(0, front_y + 28, W, front_y + 33), fill(shade(th["accent"], 1.5)))
    # Tassen / Dekoration auf dem Tisch
    for x, col in ((540 - 30, "#f2f2f2"), (540 + 34, th["accent2"])):
        c.drawPath(rrect(x - 18, DESK_Y + 2, 36, 46, 8), fill(col))
        c.drawPath(oval(x, DESK_Y + 4, 18, 6), fill(shade(col, 0.6)))
        c.drawPath(path_from([("M", x + 18, DESK_Y + 14), ("Q", x + 34, DESK_Y + 24, x + 18, DESK_Y + 36)],
                             closed=False), stroke(col, 6))


def _draw_mic(c: skia.Canvas, s: Slot) -> None:
    k = s.scale
    inner = s.inner
    base = (s.x + inner * 118 * k, DESK_Y + 52 * k)
    joint = (s.x + inner * 132 * k, DESK_Y - 150 * k)
    mic = (s.x + inner * 70 * k, DESK_Y - 256 * k)
    dark, mid = "#16161b", "#34343d"
    c.drawPath(oval(base[0], base[1], 56 * k, 15 * k), fill("#000000", 0.35))
    c.drawPath(oval(base[0], base[1] - 4 * k, 50 * k, 13 * k), fill(mid))
    arm = path_from([("M", *base), ("L", *joint), ("L", *mic)], closed=False)
    c.drawPath(arm, stroke(dark, 13 * k))
    c.drawPath(arm, stroke("#55555f", 4 * k))
    c.drawCircle(*joint, 10 * k, fill(mid))
    # Mikrofon-Körper zeigt Richtung Mund
    mouth = (s.x, DESK_Y - 345 * k)
    ang = math.degrees(math.atan2(mouth[1] - mic[1], mouth[0] - mic[0]))
    c.save()
    c.translate(*mic)
    c.rotate(ang)
    c.scale(k, k)
    c.drawPath(rrect(-62, -27, 92, 54, 24), fill(dark))
    c.drawPath(rrect(-62, -27, 92, 54, 24), stroke("#000000", 3))
    c.drawPath(rrect(14, -31, 46, 62, 26), fill("#2a2a31"))  # Schaumstoff-Popschutz
    for i in range(4):
        c.drawCircle(24 + i * 9, -12 + (i % 2) * 14, 2.5, fill("#45454f"))
    c.drawPath(rrect(-50, -20, 52, 8, 4), fill("#ffffff", 0.12))
    c.restore()


# --------------------------------------------------------------------------- Kamera

@dataclass
class Shot:
    start: float
    end: float
    kind: str          # 'wide' | 'close' | 'medium'
    target: int = -1   # Slot-Index bei close/medium


def plan_shots(spk_segments: list[tuple[float, float, int]], duration: float, n_slots: int,
               seed: int = 1, min_shot: float = 1.7) -> list[Shot]:
    """Schnittplan wie bei einem echten Video-Podcast: Nahaufnahme auf den Sprecher, Totale bei schnellen Wechseln."""
    rng = random.Random(seed)
    if n_slots < 2 or not spk_segments:
        return [Shot(0.0, duration, "medium" if n_slots == 1 else "wide", 0)]
    # Redebeiträge zusammenfassen
    turns: list[list] = []
    for s, e, k in spk_segments:
        if turns and turns[-1][2] == k and s - turns[-1][1] < 0.7:
            turns[-1][1] = e
        else:
            turns.append([s, e, k])
    shots: list[Shot] = []
    intro = min(1.4, duration)
    shots.append(Shot(0.0, intro, "wide"))
    t = intro
    for i, (s, e, k) in enumerate(turns):
        if e <= t + 0.3:
            continue
        start = max(t, s - 0.12)
        length = e - start
        if length < min_shot:
            # kurzer Einwurf: wenn viel hin und her, Totale zeigen
            if shots[-1].kind != "wide" and i + 1 < len(turns) and turns[i + 1][0] - e < 1.0:
                shots.append(Shot(start, e, "wide"))
                t = e
            continue
        # lange Beiträge in Abschnitte teilen (Nah / Halbnah / Totale mit Reaktion)
        cur = start
        variants = ["close", "medium", "close", "wide"]
        vi = 0
        while e - cur > 0.2:
            piece = min(e - cur, rng.uniform(4.5, 7.0))
            if e - (cur + piece) < 2.0:
                piece = e - cur
            kind = variants[vi % len(variants)] if piece > 0 else "close"
            if kind == "wide" and piece > 3.5:
                piece = 2.6
            shots.append(Shot(cur, cur + piece, kind, k))
            cur += piece
            vi += 1
        t = e
    # Lücken füllen / verschmelzen
    out: list[Shot] = []
    for sh in shots:
        if out and sh.start > out[-1].end:
            out[-1].end = sh.start
        if out and out[-1].kind == sh.kind and out[-1].target == sh.target:
            out[-1].end = sh.end
            continue
        out.append(sh)
    out[-1].end = max(out[-1].end, duration)
    # zu kurze Shots an den Vorgänger anhängen
    merged: list[Shot] = []
    for sh in out:
        if merged and sh.end - sh.start < 0.9:
            merged[-1].end = sh.end
        else:
            merged.append(sh)
    return merged


def camera_for(shot: Shot, slots: list[Slot], t: float) -> tuple[float, float, float]:
    """(cx, cy, zoom) in Weltkoordinaten; leichter, langsamer Zoom innerhalb eines Shots."""
    p = (t - shot.start) / max(0.1, shot.end - shot.start)
    if shot.kind == "wide" or shot.target < 0 or shot.target >= len(slots):
        # Totale bleibt ruhig (zoom 1.0 = schnellster Renderpfad)
        return W / 2, H / 2, 1.0
    s = slots[shot.target]
    hx, hy = s.head
    if shot.kind == "close":
        z = (1.72 + 0.06 * p) * (1.0 / s.scale) ** 0.5
        cy = hy + 60 * s.scale
    else:
        z = (1.34 + 0.05 * p) * (1.0 / s.scale) ** 0.5
        cy = hy + 120 * s.scale
    return hx, cy, z


def clamp_camera(cx: float, cy: float, z: float, vw: float = W, vh: float = H) -> tuple[float, float, float]:
    z = max(1.0, z)
    hw, hh = vw / 2 / z, vh / 2 / z
    cx = min(max(cx, hw), W - hw)
    cy = min(max(cy, hh), H - hh)
    return cx, cy, z


def emphasis_events(env: np.ndarray, fps: float, min_gap: float = 2.5) -> list[int]:
    """Frames mit plötzlicher Lautstärke-Spitze (für kurze 'Punch-in'-Zooms)."""
    if len(env) == 0:
        return []
    win = int(fps * 1.2)
    events, last = [], -10 ** 9
    for f in range(len(env)):
        base = float(np.median(env[max(0, f - win): f + 1]))
        if env[f] > 1.05 and env[f] > base * 1.7 and f - last > min_gap * fps:
            events.append(f)
            last = f
    return events
