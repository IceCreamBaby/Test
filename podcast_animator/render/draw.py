"""Kleine Hilfsfunktionen rund um skia (Farben, Pinsel, Pfade, Schriften)."""
from __future__ import annotations

import math
from functools import lru_cache

import skia

from ..paths import FONTS_DIR


def rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def color(hex_color: str, alpha: float = 1.0) -> int:
    r, g, b = rgb(hex_color)
    return skia.ColorSetARGB(int(max(0, min(1, alpha)) * 255), r, g, b)


def to_hex(r: float, g: float, b: float) -> str:
    return "#%02x%02x%02x" % tuple(int(max(0, min(255, round(v)))) for v in (r, g, b))


def shade(hex_color: str, f: float) -> str:
    """f < 1 dunkler, f > 1 heller."""
    r, g, b = rgb(hex_color)
    if f <= 1:
        return to_hex(r * f, g * f, b * f)
    k = f - 1
    return to_hex(r + (255 - r) * k, g + (255 - g) * k, b + (255 - b) * k)


def mix(c1: str, c2: str, t: float) -> str:
    a, b = rgb(c1), rgb(c2)
    return to_hex(*(x + (y - x) * t for x, y in zip(a, b)))


def fill(hex_color: str, alpha: float = 1.0) -> skia.Paint:
    return skia.Paint(Color=color(hex_color, alpha), AntiAlias=True, Style=skia.Paint.kFill_Style)


def stroke(hex_color: str, width: float, alpha: float = 1.0, cap=skia.Paint.kRound_Cap) -> skia.Paint:
    return skia.Paint(Color=color(hex_color, alpha), AntiAlias=True, Style=skia.Paint.kStroke_Style,
                      StrokeWidth=width, StrokeCap=cap, StrokeJoin=skia.Paint.kRound_Join)


def blurred(paint: skia.Paint, sigma: float) -> skia.Paint:
    paint.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, sigma))
    return paint


def linear(paint: skia.Paint, p0, p1, colors: list[str], pos: list[float] | None = None) -> skia.Paint:
    paint.setShader(skia.GradientShader.MakeLinear(
        points=[skia.Point(*p0), skia.Point(*p1)], colors=[color(c) for c in colors], positions=pos))
    return paint


def radial(paint: skia.Paint, center, radius, colors: list[int], pos: list[float] | None = None) -> skia.Paint:
    paint.setShader(skia.GradientShader.MakeRadial(
        center=skia.Point(*center), radius=radius, colors=colors, positions=pos))
    return paint


def path_from(points, closed: bool = True) -> skia.Path:
    """Pfad aus Kommandos: ('M', x, y) ('L', x, y) ('C', x1, y1, x2, y2, x, y) ('Q', x1, y1, x, y)."""
    p = skia.Path()
    for cmd in points:
        op, *a = cmd
        if op == "M":
            p.moveTo(*a)
        elif op == "L":
            p.lineTo(*a)
        elif op == "C":
            p.cubicTo(*a)
        elif op == "Q":
            p.quadTo(*a)
    if closed:
        p.close()
    return p


def smooth_closed(points: list[tuple[float, float]], tension: float = 0.5) -> skia.Path:
    """Geschlossene, weiche Kurve durch alle Punkte (Catmull-Rom -> Bezier)."""
    n = len(points)
    p = skia.Path()
    p.moveTo(*points[0])
    for i in range(n):
        p0, p1, p2, p3 = points[(i - 1) % n], points[i], points[(i + 1) % n], points[(i + 2) % n]
        c1 = (p1[0] + (p2[0] - p0[0]) * tension / 3, p1[1] + (p2[1] - p0[1]) * tension / 3)
        c2 = (p2[0] - (p3[0] - p1[0]) * tension / 3, p2[1] - (p3[1] - p1[1]) * tension / 3)
        p.cubicTo(*c1, *c2, *p2)
    p.close()
    return p


def smooth_open(points: list[tuple[float, float]], tension: float = 0.5) -> skia.Path:
    n = len(points)
    p = skia.Path()
    p.moveTo(*points[0])
    for i in range(n - 1):
        p0 = points[max(0, i - 1)]
        p1, p2 = points[i], points[i + 1]
        p3 = points[min(n - 1, i + 2)]
        c1 = (p1[0] + (p2[0] - p0[0]) * tension / 3, p1[1] + (p2[1] - p0[1]) * tension / 3)
        c2 = (p2[0] - (p3[0] - p1[0]) * tension / 3, p2[1] - (p3[1] - p1[1]) * tension / 3)
        p.cubicTo(*c1, *c2, *p2)
    return p


def blob(points: list[tuple[float, float]], sharp: tuple[int, ...] | set[int] = ()) -> skia.Path:
    """Weiche Form innerhalb des Kontrollpolygons; Indizes in `sharp` werden zu spitzen Ecken."""
    n = len(points)
    mids = [((points[i][0] + points[(i + 1) % n][0]) / 2, (points[i][1] + points[(i + 1) % n][1]) / 2)
            for i in range(n)]
    p = skia.Path()
    p.moveTo(*mids[-1])
    for i in range(n):
        if i in sharp:
            p.lineTo(*points[i])
            p.lineTo(*mids[i])
        else:
            p.quadTo(*points[i], *mids[i])
    p.close()
    return p


def oval(cx: float, cy: float, rx: float, ry: float) -> skia.Path:
    p = skia.Path()
    p.addOval(skia.Rect.MakeLTRB(cx - rx, cy - ry, cx + rx, cy + ry))
    return p


def rrect(x: float, y: float, w: float, h: float, r: float) -> skia.Path:
    p = skia.Path()
    p.addRRect(skia.RRect.MakeRectXY(skia.Rect.MakeXYWH(x, y, w, h), r, r))
    return p


def ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def ease_out_back(t: float, s: float = 1.7) -> float:
    t = max(0.0, min(1.0, t)) - 1
    return t * t * ((s + 1) * t + s) + 1


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def rot(x: float, y: float, deg: float) -> tuple[float, float]:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return x * c - y * s, x * s + y * c


@lru_cache(maxsize=16)
def typeface(name: str) -> skia.Typeface:
    path = FONTS_DIR / name
    if path.exists():
        tf = skia.Typeface.MakeFromFile(str(path))
        if tf is not None:
            return tf
    return skia.Typeface("Arial", skia.FontStyle.Bold())


def font(name: str, size: float) -> skia.Font:
    f = skia.Font(typeface(name), size)
    f.setEdging(skia.Font.Edging.kAntiAlias)
    f.setSubpixel(True)
    return f
