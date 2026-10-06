"""Figuren: prozedural gezeichnete Cartoon-Figuren (Vektor) und eigene PNG-Figuren (Sprites).

Lokales Koordinatensystem einer Figur (Maßstab 1): Ursprung = Tischkante mittig unter der Figur,
negative y-Werte liegen oberhalb des Tisches. Der Kopf ist ca. 200 px breit.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import skia

from ..paths import BUILTIN_CHARACTERS_DIR, USER_CHARACTERS_DIR
from .draw import blob, ease, fill, lerp, mix, oval, path_from, rrect, shade, smooth_closed, smooth_open, stroke

OUTLINE = "#1c1922"
OUTLINE_W = 4.5

DEFAULT_STYLE = {
    "id": "figur",
    "name": "Figur",
    "type": "vector",
    "color": "#ffcc00",
    "skin": "#f3c9a5",
    "hair": "#3b2a20",
    "hair_style": "messy",
    "eye_color": "#5a3b25",
    "brows": None,
    "outfit": "tshirt",
    "outfit_color": "#4a6fa5",
    "outfit_accent": "#ffffff",
    "headphones": True,
    "headphones_color": "#26262b",
    "glasses": False,
    "glasses_color": "#1c1922",
    "beard": "none",
    "earring": False,
    "chain": False,
    "cap": False,
    "cap_color": "#202020",
    "face_width": 1.0,
    "scale": 1.0,
}


# --------------------------------------------------------------------------- Zustand pro Frame

@dataclass
class CharState:
    talk: float = 0.0          # Mundöffnung 0..1
    viseme: str = "rest"       # A, E, O, C, F, M, rest
    blink: float = 0.0         # 0 = offen, 1 = zu
    gaze_x: float = 0.0        # Blickrichtung -1..1
    gaze_y: float = 0.0
    head_dx: float = 0.0
    head_dy: float = 0.0
    head_rot: float = 0.0      # Grad
    body_dy: float = 0.0
    brow: float = 0.0          # 1 = hochgezogen, -1 = runzeln
    laugh: float = 0.0         # 0..1
    smile: float = 0.3         # Grundlächeln
    facing: float = 0.0        # Kopfdrehung -1 (links) .. 1 (rechts)
    gesture_l: float = 0.0     # Arm heben 0..1
    gesture_r: float = 0.0
    gesture_phase: float = 0.0
    speaking: bool = False


# --------------------------------------------------------------------------- Laden / Speichern

def list_characters() -> list[dict]:
    chars: dict[str, dict] = {}
    for d in (BUILTIN_CHARACTERS_DIR, USER_CHARACTERS_DIR):
        if not d.exists():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            data.setdefault("id", f.stem)
            data["_dir"] = str(f.parent)
            data["_builtin"] = d == BUILTIN_CHARACTERS_DIR
            chars[data["id"]] = data
        # Sprite-Figuren als Unterordner mit character.json
        for sub in sorted(p for p in d.iterdir() if p.is_dir()):
            cj = sub / "character.json"
            if cj.exists():
                try:
                    data = json.loads(cj.read_text(encoding="utf-8"))
                except Exception:
                    continue
                data.setdefault("id", sub.name)
                data["_dir"] = str(sub)
                data["_builtin"] = d == BUILTIN_CHARACTERS_DIR
                chars[data["id"]] = data
    return list(chars.values())


def get_character(char_id: str) -> dict:
    for c in list_characters():
        if c["id"] == char_id:
            return {**DEFAULT_STYLE, **c}
    return {**DEFAULT_STYLE, "id": char_id, "name": char_id.capitalize()}


def save_character(data: dict) -> dict:
    USER_CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in data.items() if not k.startswith("_")}
    char_id = "".join(ch for ch in clean.get("id", "figur").lower() if ch.isalnum() or ch in "-_") or "figur"
    clean["id"] = char_id
    (USER_CHARACTERS_DIR / f"{char_id}.json").write_text(json.dumps(clean, indent=2, ensure_ascii=False),
                                                         encoding="utf-8")
    return get_character(char_id)


def make_character(style: dict):
    if style.get("type") == "sprite":
        return SpriteCharacter(style)
    return VectorCharacter(style)


# --------------------------------------------------------------------------- Vektor-Figur

class VectorCharacter:
    """Cartoon-Figur, komplett mit Vektoren gezeichnet und über `style` anpassbar."""

    HEAD_CY = -420.0
    NECK_Y = -300.0

    def __init__(self, style: dict):
        self.s = {**DEFAULT_STYLE, **style}
        s = self.s
        self.skin = s["skin"]
        self.skin_sh = shade(s["skin"], 0.86)
        self.hair = s["hair"]
        self.hair_sh = shade(s["hair"], 0.72)
        self.hair_hi = shade(s["hair"], 1.25)
        self.brows = s.get("brows") or shade(s["hair"], 0.8)
        self.outfit = s["outfit_color"]
        self.outfit_sh = shade(s["outfit_color"], 0.78)
        self.outfit_hi = shade(s["outfit_color"], 1.15)
        self.fw = float(s.get("face_width", 1.0))
        self.scale = float(s.get("scale", 1.0))

    # ---------------------------------------------------------------- Helfer
    @staticmethod
    def _shape(c: skia.Canvas, path: skia.Path, hex_fill: str, outline: bool = True,
               width: float = OUTLINE_W, alpha: float = 1.0) -> None:
        c.drawPath(path, fill(hex_fill, alpha))
        if outline:
            c.drawPath(path, stroke(OUTLINE, width))

    @staticmethod
    def _limb(c: skia.Canvas, pts, width: float, hex_fill: str) -> None:
        p = smooth_open(pts) if len(pts) > 2 else path_from([("M", *pts[0]), ("L", *pts[1])], closed=False)
        c.drawPath(p, stroke(OUTLINE, width + OUTLINE_W * 2))
        c.drawPath(p, stroke(hex_fill, width))

    # ---------------------------------------------------------------- Hauptaufrufe
    def draw_body(self, c: skia.Canvas, st: CharState) -> None:
        """Alles hinter dem Tisch: Oberkörper, Kopf, Haare."""
        c.save()
        c.scale(self.scale, self.scale)
        c.translate(0, st.body_dy)
        self._torso(c, st)
        c.save()
        c.translate(st.head_dx, st.head_dy)
        c.rotate(st.head_rot, 0, self.NECK_Y)
        self._head(c, st)
        c.restore()
        c.restore()

    def draw_front(self, c: skia.Canvas, st: CharState, inner: int) -> None:
        """Arme und Hände auf dem Tisch (werden nach dem Tisch gezeichnet). inner = Richtung Bildmitte."""
        c.save()
        c.scale(self.scale, self.scale)
        c.translate(0, st.body_dy * 0.5)
        for side in (-1, 1):
            g = st.gesture_l if side == -1 else st.gesture_r
            self._arm(c, side, ease(g), st)
        c.restore()

    def head_anchor(self) -> tuple[float, float]:
        return 0.0, self.HEAD_CY * self.scale

    # ---------------------------------------------------------------- Oberkörper
    def _torso(self, c: skia.Canvas, st: CharState) -> None:
        s = self.s
        outfit = s["outfit"]
        # Kapuze / Kragen hinter dem Hals
        if outfit == "hoodie":
            hood = smooth_closed([(-92, -262), (-70, -300), (0, -318), (70, -300), (92, -262), (0, -246)], 0.7)
            self._shape(c, hood, self.outfit_sh)
        # Hals
        neck = path_from([("M", -31, -335), ("L", 31, -335), ("L", 34, -235), ("L", -34, -235)])
        self._shape(c, neck, self.skin)
        c.drawPath(path_from([("M", -31, -320), ("Q", 0, -296, 31, -320), ("L", 31, -300),
                              ("Q", 0, -276, -31, -300)]), fill(self.skin_sh))
        # Rumpf
        body = path_from([
            ("M", -58, -268),
            ("C", -112, -262, -160, -252, -174, -205),
            ("L", -196, 80), ("L", 196, 80), ("L", 174, -205),
            ("C", 160, -252, 112, -262, 58, -268),
            ("Q", 0, -228, -58, -268),
        ])
        c.save()
        self._shape(c, body, self.outfit)
        c.clipPath(body, doAntiAlias=True)
        # leichte Schattierung
        sh = fill(self.outfit_sh, 0.55)
        c.drawPath(path_from([("M", -200, -150), ("Q", -140, -60, -150, 90), ("L", -210, 90)]), sh)
        c.drawPath(path_from([("M", 200, -150), ("Q", 140, -60, 150, 90), ("L", 210, 90)]), sh)
        if outfit == "hoodie":
            pocket = path_from([("M", -95, 20), ("L", -70, -45), ("L", 70, -45), ("L", 95, 20)])
            c.drawPath(pocket, stroke(self.outfit_sh, 4))
        elif outfit == "jacket":
            inner = path_from([("M", -50, -262), ("L", 50, -262), ("L", 26, 90), ("L", -26, 90)])
            c.drawPath(inner, fill(s["outfit_accent"]))
            c.drawPath(inner, stroke(OUTLINE, 3))
            for side in (-1, 1):
                lapel = path_from([("M", side * 52, -264), ("L", side * 30, -150), ("L", side * 70, -190),
                                   ("L", side * 92, -255)])
                c.drawPath(lapel, fill(self.outfit_hi))
                c.drawPath(lapel, stroke(OUTLINE, 3))
        elif outfit == "shirt":
            c.drawPath(path_from([("M", 0, -238), ("L", 0, 90)], closed=False), stroke(self.outfit_sh, 4))
            for y in (-190, -130, -70, -10):
                c.drawCircle(6, y, 4.5, fill(self.outfit_sh))
        elif outfit == "tshirt" and s.get("print"):
            c.drawPath(rrect(-46, -190, 92, 60, 14), fill(s["outfit_accent"], 0.9))
        c.restore()
        # Halsausschnitt / Kragen
        if outfit == "tshirt":
            c.drawPath(path_from([("M", -58, -268), ("Q", 0, -222, 58, -268)], closed=False),
                       stroke(self.outfit_sh, 9))
        elif outfit == "shirt":
            for side in (-1, 1):
                col = path_from([("M", side * 6, -236), ("L", side * 58, -270), ("L", side * 66, -222)])
                self._shape(c, col, self.outfit_hi, width=3.5)
        elif outfit == "hoodie":
            for side in (-1, 1):
                c.drawPath(path_from([("M", side * 20, -246), ("Q", side * 26, -200, side * 22, -165)],
                                     closed=False), stroke(s["outfit_accent"], 6))
                c.drawPath(rrect(side * 22 - 5, -168, 10, 18, 4), fill(shade(s["outfit_accent"], 0.85)))
        if s.get("chain"):
            c.drawPath(path_from([("M", -40, -262), ("Q", 0, -200, 40, -262)], closed=False),
                       stroke("#e8c547", 5))
        c.drawPath(body, stroke(OUTLINE, OUTLINE_W))

    # ---------------------------------------------------------------- Kopf
    def _head_path(self) -> skia.Path:
        fw = self.fw
        return path_from([
            ("M", 0, -545),
            ("C", 62 * fw, -545, 102 * fw, -500, 102 * fw, -430),
            ("C", 102 * fw, -372, 88 * fw, -332, 54 * fw, -310),
            ("C", 32 * fw, -296, -32 * fw, -296, -54 * fw, -310),
            ("C", -88 * fw, -332, -102 * fw, -372, -102 * fw, -430),
            ("C", -102 * fw, -500, -62 * fw, -545, 0, -545),
        ])

    def _head(self, c: skia.Canvas, st: CharState) -> None:
        s = self.s
        fx = st.facing * 9.0
        self._hair_back(c, st)
        if s.get("headphones"):
            self._headphones(c, st, part="band")
        # Ohren
        for side in (-1, 1):
            k = 1.0 - 0.25 * max(0.0, side * st.facing)
            ex = side * (99 * self.fw) - st.facing * 4
            ear = oval(ex, -418, 17 * k, 30)
            self._shape(c, ear, self.skin)
            c.drawPath(path_from([("M", ex + side * 3, -432), ("Q", ex + side * 9 * k, -418, ex + side * 2, -404)],
                                 closed=False), stroke(self.skin_sh, 4))
            if s.get("earring"):
                c.drawCircle(ex + side * 2, -390, 5, fill("#e8c547"))
        head = self._head_path()
        c.save()
        self._shape(c, head, self.skin)
        c.clipPath(head, doAntiAlias=True)
        # weicher Schatten am Kinn und unter dem Haaransatz
        c.drawPath(oval(fx * 0.5, -296, 70 * self.fw, 26), fill(self.skin_sh, 0.55))
        c.drawPath(oval(fx * 0.4, -505, 110, 40), fill(self.skin_sh, 0.45))
        c.restore()
        if s.get("beard") in ("stubble", "full"):
            self._beard(c, st, fx)
        self._face(c, st, fx)
        self._hair_front(c, st)
        if s.get("cap"):
            self._cap(c, st)
        if s.get("glasses"):
            self._glasses(c, st, fx)
        if s.get("headphones"):
            self._headphones(c, st, part="cups")

    def _face(self, c: skia.Canvas, st: CharState, fx: float) -> None:
        s = self.s
        # Rouge
        for side in (-1, 1):
            c.drawPath(oval(side * 60 + fx, -372, 17, 9), fill("#ff7a7a", 0.22))
        # Augen
        eye_y = -425.0
        for side in (-1, 1):
            ex = side * 40 + fx
            if st.laugh > 0.5:
                c.drawPath(path_from([("M", ex - 16, eye_y + 6), ("Q", ex, eye_y - 16, ex + 16, eye_y + 6)],
                                     closed=False), stroke(OUTLINE, 6))
                continue
            open_ = max(0.0, 1.0 - st.blink)
            if open_ < 0.15:
                c.drawPath(path_from([("M", ex - 17, eye_y + 2), ("Q", ex, eye_y + 10, ex + 17, eye_y + 2)],
                                     closed=False), stroke(OUTLINE, 5.5))
                continue
            rx, ry = 18.0, 22.0 * open_
            white = oval(ex, eye_y, rx, ry)
            c.save()
            c.drawPath(white, fill("#ffffff"))
            c.clipPath(white, doAntiAlias=True)
            ix = ex + st.gaze_x * 6.5
            iy = eye_y + st.gaze_y * 5 + 2
            c.drawCircle(ix, iy, 12.5, fill(s["eye_color"]))
            c.drawCircle(ix, iy, 6.5, fill("#120d10"))
            c.drawCircle(ix - 4, iy - 5, 3.6, fill("#ffffff"))
            # Oberlid-Schatten
            c.drawPath(oval(ex, eye_y - ry - 6, rx + 4, 10), fill(self.skin_sh, 0.5))
            c.restore()
            c.drawPath(white, stroke(OUTLINE, 3.5))
            # Wimpernlinie oben
            c.drawPath(path_from([("M", ex - rx - 1, eye_y - 2), ("Q", ex, eye_y - ry * 1.45 - 1, ex + rx + 1, eye_y - 2)],
                                 closed=False), stroke(OUTLINE, 5))
        # Augenbrauen
        for side in (-1, 1):
            bx = side * 41 + fx
            by = -466 - st.brow * 9 - st.laugh * 4
            tilt = (st.brow * -3 if st.brow > 0 else st.brow * -7) * side
            p = path_from([("M", bx - 20, by + 4 - tilt), ("Q", bx, by - 6, bx + 20, by + 4 + tilt)], closed=False)
            c.drawPath(p, stroke(self.brows, 9))
        # Nase
        nx = fx * 1.2
        c.drawPath(path_from([("M", nx - 2, -405), ("Q", nx + 10, -384, nx - 5, -380)], closed=False),
                   stroke(self.skin_sh, 5))
        self._mouth(c, st, fx * 0.9)

    def _mouth(self, c: skia.Canvas, st: CharState, fx: float) -> None:
        mx, my = fx, -347.0
        talk = max(0.0, min(1.0, st.talk))
        if st.laugh > 0.3:
            w, h = 58.0, 26 + 12 * talk
            p = path_from([("M", mx - w / 2, my - 4), ("Q", mx, my + 2, mx + w / 2, my - 4),
                           ("C", mx + w / 2.3, my + h, mx - w / 2.3, my + h, mx - w / 2, my - 4)])
            self._mouth_inside(c, p, mx, my, w, h, teeth=True)
            return
        vis = st.viseme if talk > 0.08 else "rest"
        if vis in ("rest", "M"):
            sm = st.smile * 7
            w = 40.0 if vis == "rest" else 32.0
            p = path_from([("M", mx - w / 2, my - sm * 0.3), ("Q", mx, my + sm, mx + w / 2, my - sm * 0.3)],
                          closed=False)
            c.drawPath(p, stroke(OUTLINE, 5))
            return
        shapes = {
            "A": (42.0, 12 + 30 * talk),
            "E": (50.0, 8 + 15 * talk),
            "O": (27.0, 15 + 20 * talk),
            "C": (36.0, 7 + 14 * talk),
            "F": (36.0, 9.0),
        }
        w, h = shapes.get(vis, shapes["C"])
        top = -h * 0.32
        p = path_from([("M", mx - w / 2, my), ("C", mx - w / 4, my + top, mx + w / 4, my + top, mx + w / 2, my),
                       ("C", mx + w / 2.1, my + h * 0.8, mx - w / 2.1, my + h * 0.8, mx - w / 2, my)])
        self._mouth_inside(c, p, mx, my + top * 0.4, w, h, teeth=vis in ("E", "F", "C", "A"))

    def _mouth_inside(self, c, p, mx, my, w, h, teeth: bool) -> None:
        c.save()
        c.drawPath(p, fill("#5b1823"))
        c.clipPath(p, doAntiAlias=True)
        if teeth:
            c.drawRect(skia.Rect.MakeLTRB(mx - w, my - h, mx + w, my + min(7.0, h * 0.28)), fill("#ffffff"))
        c.drawPath(oval(mx, my + h * 0.75, w * 0.36, h * 0.35), fill("#e2717f"))
        c.restore()
        c.drawPath(p, stroke(OUTLINE, 4.5))

    def _beard(self, c: skia.Canvas, st: CharState, fx: float) -> None:
        fw = self.fw
        full = self.s.get("beard") == "full"
        col = self.hair if full else mix(self.hair, self.skin, 0.35)
        alpha = 0.95 if full else 0.28
        jaw = path_from([
            ("M", -101 * fw, -415),
            ("C", -100 * fw, -360, -86 * fw, -326, -54 * fw, -308),
            ("C", -32 * fw, -294, 32 * fw, -294, 54 * fw, -308),
            ("C", 86 * fw, -326, 100 * fw, -360, 101 * fw, -415),
            ("C", 92 * fw, -380, 74 * fw, -350, 46 + fx, -340),
            ("C", 30 + fx, -318, -30 + fx, -318, -46 + fx, -340),
            ("C", -74 * fw, -350, -92 * fw, -380, -101 * fw, -415),
        ])
        stache = path_from([("M", -30 + fx, -360), ("Q", fx, -374, 30 + fx, -360), ("Q", 34 + fx, -352, 26 + fx, -352),
                            ("Q", fx, -362, -26 + fx, -352), ("Q", -34 + fx, -352, -30 + fx, -360)])
        c.save()
        c.clipPath(self._head_path(), doAntiAlias=True)
        c.drawPath(jaw, fill(col, alpha))
        c.drawPath(stache, fill(col, alpha * 1.1))
        c.restore()

    # ---------------------------------------------------------------- Haare
    def _hair_back(self, c: skia.Canvas, st: CharState) -> None:
        style = self.s["hair_style"]
        fw = self.fw
        if style == "long":
            p = blob([(-124 * fw, -440), (-118, -560), (0, -605), (118, -560), (124 * fw, -440),
                      (134 * fw, -300), (118 * fw, -240), (-118 * fw, -240), (-134 * fw, -300)])
            self._shape(c, p, self.hair_sh)
        elif style == "bun":
            self._shape(c, oval(0, -592, 44, 38), self.hair)
        elif style == "swoop":
            self._shape(c, blob([(-120 * fw, -415), (-132 * fw, -530), (-90, -615), (10, -645), (110, -615),
                                 (146 * fw, -530), (134 * fw, -415)]), self.hair_sh)
        elif style == "quiff":
            self._shape(c, blob([(-110 * fw, -440), (-116 * fw, -540), (-60, -625), (50, -650), (126, -615),
                                 (128 * fw, -520), (114 * fw, -440)]), self.hair_sh)
        elif style in ("messy", "curly"):
            self._shape(c, blob([(-118 * fw, -420), (-130 * fw, -520), (-70, -610), (40, -620), (128 * fw, -540),
                                 (122 * fw, -420)]), self.hair_sh)

    def _strands(self, c: skia.Canvas, lines, hi_line) -> None:
        for pts in lines:
            c.drawPath(smooth_open(pts), stroke(self.hair_sh, 4.5))
        if hi_line:
            c.drawPath(smooth_open(hi_line), stroke(self.hair_hi, 7, alpha=0.9))

    def _hair_front(self, c: skia.Canvas, st: CharState) -> None:
        style = self.s["hair_style"]
        fw = self.fw
        sx = st.facing * 5
        if style == "bald":
            c.drawPath(oval(-35 + sx, -515, 22, 10), fill("#ffffff", 0.25))
            return
        if style == "buzz":
            p = path_from([
                ("M", -103 * fw, -440), ("C", -104 * fw, -510, -60, -552, 0, -552),
                ("C", 60, -552, 104 * fw, -510, 103 * fw, -440),
                ("C", 90 * fw, -470, 60, -488, 0 + sx, -490), ("C", -60, -488, -90 * fw, -470, -103 * fw, -440),
            ])
            self._shape(c, p, self.hair, alpha=0.95)
            return
        if style == "swoop":
            # viel Volumen oben, spitzer Pony schwungvoll zur Seite
            pts = [(-102 * fw, -410), (-118 * fw, -470), (-122 * fw, -545), (-92, -608), (-28, -638),
                   (48, -634), (114, -600), (142 * fw, -535), (134 * fw, -462), (110 * fw, -420),
                   (104 * fw + sx, -458), (100 + sx, -424), (78 + sx, -478), (50 + sx, -450), (32 + sx, -494),
                   (2 + sx, -468), (-18 + sx, -506), (-70 + sx, -502), (-98 * fw, -470)]
            self._shape(c, blob(pts, sharp={0, 9, 11, 13, 15}), self.hair)
            self._strands(c, [
                [(-60 + sx, -590), (10 + sx, -582), (70 + sx, -530), (96 + sx, -440)],
                [(-90 + sx, -540), (-20 + sx, -540), (40 + sx, -505), (52 + sx, -460)],
                [(20 + sx, -615), (90 + sx, -590), (120 + sx, -520)],
            ], [(-50 + sx, -612), (20 + sx, -616), (80 + sx, -590)])
            return
        if style == "quiff":
            # hohe, nach hinten gestylte Tolle, kurze Seiten
            for side in (-1, 1):
                fade = blob([(side * 100 * fw, -412), (side * 108 * fw, -500), (side * 70, -520),
                             (side * 92 * fw, -470)], sharp={0})
                c.drawPath(fade, fill(self.hair, 0.55))
            pts = [(-96 * fw, -470), (-104 * fw, -540), (-86, -598), (-36, -636), (34, -652), (100, -636),
                   (128, -598), (120 * fw, -540), (104 * fw, -470), (88 * fw + sx, -502), (60 + sx, -520),
                   (20 + sx, -524), (-20 + sx, -520), (-60 + sx, -510)]
            self._shape(c, blob(pts, sharp={0, 8}), self.hair)
            self._strands(c, [
                [(-62 + sx, -530), (-40 + sx, -586), (10 + sx, -622), (66 + sx, -636)],
                [(-20 + sx, -528), (10 + sx, -578), (60 + sx, -608), (108 + sx, -612)],
                [(30 + sx, -526), (60 + sx, -566), (106 + sx, -584)],
            ], [(-40 + sx, -612), (10 + sx, -636), (64 + sx, -642)])
            return
        if style == "long":
            pts = [(-112 * fw, -320), (-122 * fw, -480), (-86, -575), (0, -602), (86, -575), (122 * fw, -480),
                   (112 * fw, -320), (96 * fw, -380), (90 * fw, -470), (40 + sx, -505), (0 + sx, -528),
                   (-40 + sx, -505), (-90 * fw, -470), (-96 * fw, -380)]
            self._shape(c, blob(pts, sharp={0, 6, 10}), self.hair)
            self._strands(c, [[(0 + sx, -596), (-2 + sx, -530)]], [(-60 + sx, -570), (0 + sx, -590), (60 + sx, -570)])
            return
        if style == "curly":
            pts = []
            for i in range(17):
                a = math.pi * (1.0 + i / 16)
                r = 134 if i % 2 == 0 else 112
                pts.append((math.cos(a) * r * fw, -450 + math.sin(a) * r * 1.05))
            pts += [(96 * fw, -440), (50 + sx, -486), (0 + sx, -476), (-50 + sx, -486), (-96 * fw, -440)]
            self._shape(c, smooth_closed(pts, 0.8), self.hair)
            return
        # messy (Standard): wuschelige Strähnen
        pts = [(-104 * fw, -412), (-124 * fw, -480), (-112, -545), (-132, -580), (-74, -592), (-44, -634),
               (0, -604), (44, -636), (70, -594), (128, -588), (116 * fw, -536), (130 * fw, -478), (106 * fw, -412),
               (92 * fw, -466), (60 + sx, -486), (36 + sx, -462), (6 + sx, -490), (-30 + sx, -464),
               (-60 + sx, -490), (-92 * fw, -466)]
        self._shape(c, blob(pts, sharp={0, 3, 5, 7, 9, 12, 15, 17}), self.hair)
        self._strands(c, [], [(-50 + sx, -570), (0 + sx, -584), (50 + sx, -566)])

    def _cap(self, c: skia.Canvas, st: CharState) -> None:
        col = self.s.get("cap_color", "#202020")
        sx = st.facing * 4
        crown = path_from([("M", -108, -470), ("C", -108, -560, -60, -592, 0, -592),
                           ("C", 60, -592, 108, -560, 108, -470), ("Q", 0, -488, -108, -470)])
        self._shape(c, crown, col)
        brim = path_from([("M", -112 + sx, -472), ("Q", 0 + sx, -500, 112 + sx, -472),
                          ("Q", 130 + sx, -452, 100 + sx, -446), ("Q", 0 + sx, -470, -100 + sx, -446),
                          ("Q", -130 + sx, -452, -112 + sx, -472)])
        self._shape(c, brim, shade(col, 0.8))
        c.drawCircle(0, -592, 7, fill(shade(col, 1.3)))

    def _glasses(self, c: skia.Canvas, st: CharState, fx: float) -> None:
        col = self.s.get("glasses_color", OUTLINE)
        for side in (-1, 1):
            p = rrect(side * 40 + fx - 29, -451, 58, 50, 16)
            c.drawPath(p, fill("#ffffff", 0.12))
            c.drawPath(p, stroke(col, 6))
        c.drawPath(path_from([("M", -12 + fx, -432), ("Q", fx, -440, 12 + fx, -432)], closed=False), stroke(col, 5))
        for side in (-1, 1):
            c.drawPath(path_from([("M", side * 69 + fx, -436), ("L", side * 100, -430)], closed=False), stroke(col, 5))

    def _headphones(self, c: skia.Canvas, st: CharState, part: str) -> None:
        col = self.s.get("headphones_color", "#26262b")
        if part == "band":
            top = -602
            band = path_from([("M", -114, -420), ("C", -128, top + 20, -60, top, 0, top),
                              ("C", 60, top, 128, top + 20, 114, -420)], closed=False)
            c.drawPath(band, stroke(OUTLINE, 22))
            c.drawPath(band, stroke(col, 14))
            c.drawPath(band, stroke(shade(col, 1.6), 3))
            return
        for side in (-1, 1):
            x = side * 114 - st.facing * 4
            cup = rrect(x - 21, -464, 42, 84, 19)
            self._shape(c, cup, col)
            c.drawPath(rrect(x - 6 + side * 6, -450, 12, 54, 6), fill(shade(col, 1.5), 0.6))

    # ---------------------------------------------------------------- Arme
    def _arm(self, c: skia.Canvas, side: int, g: float, st: CharState) -> None:
        shoulder = (side * 160.0, -222.0)
        elbow = (side * lerp(188, 182, g), lerp(32, 26, g))
        rest_wrist = (side * 84.0, 58.0)
        wave = math.sin(st.gesture_phase * 2.4 + side) * 9 * g
        up_wrist = (side * (128.0 + wave), -122.0 + wave * 0.5)
        wrist = (lerp(rest_wrist[0], up_wrist[0], g), lerp(rest_wrist[1], up_wrist[1], g))
        mid = ((shoulder[0] + elbow[0]) / 2 + side * 6, (shoulder[1] + elbow[1]) / 2)
        sleeve = self.outfit_hi if g > 0.05 or self.s["outfit"] != "hoodie" else self.outfit
        self._limb(c, [shoulder, mid, elbow], 66, self.outfit)
        self._limb(c, [elbow, wrist], 58, sleeve)
        # Ärmelbündchen
        dx, dy = wrist[0] - elbow[0], wrist[1] - elbow[1]
        ln = math.hypot(dx, dy) or 1
        ux, uy = dx / ln, dy / ln
        cuff_c = (wrist[0] - ux * 4, wrist[1] - uy * 4)
        c.save()
        c.translate(*cuff_c)
        c.rotate(math.degrees(math.atan2(uy, ux)))
        self._shape(c, rrect(-8, -30, 16, 60, 6), self.outfit_sh, width=3.5)
        c.restore()
        # Hand
        hx, hy = wrist[0] + ux * 22, wrist[1] + uy * 22
        c.save()
        c.translate(hx, hy)
        c.rotate(math.degrees(math.atan2(uy, ux)) + 90 + wave * 0.8)
        if g > 0.5:
            c.translate(0, -6)
            c.scale(1.3, 1.3)
            self._shape(c, _OPEN_HAND if side > 0 else _OPEN_HAND_L, self.skin, width=OUTLINE_W / 1.3)
            c.drawPath(path_from([("M", -12, 14), ("Q", 0, 20, 12, 14)], closed=False), stroke(self.skin_sh, 3))
        else:
            c.rotate(180)
            hand = smooth_closed([(-24, -12), (-26, 14), (-12, 30), (12, 30), (26, 14), (24, -12)], 0.6)
            self._shape(c, hand, self.skin)
        c.restore()


def _make_open_hand(mirror: bool) -> skia.Path:
    """Offene Hand (Handfläche zur Kamera, Finger zeigen nach oben)."""
    k = -1 if mirror else 1
    shape = rrect(-22, -14, 44, 42, 15)
    fingers = [(-15.5, -36, 11.5), (-5, -42, 12), (6, -40, 12), (16.5, -32, 11)]
    for x, top, w in fingers:
        shape = skia.Op(shape, rrect(x * k - w / 2, top, w, 34 - top * 0.1 + 6, w / 2), skia.PathOp.kUnion_PathOp)
    thumb = skia.Path()
    thumb.addRRect(skia.RRect.MakeRectXY(skia.Rect.MakeXYWH(-6, -6, 12, 34), 6, 6))
    m = skia.Matrix()
    m.setRotate(-48 * k)
    m.postTranslate(-24 * k, 6)
    thumb.transform(m)
    return skia.Op(shape, thumb, skia.PathOp.kUnion_PathOp)


_OPEN_HAND = _make_open_hand(False)
_OPEN_HAND_L = _make_open_hand(True)


# --------------------------------------------------------------------------- Sprite-Figur (eigene PNGs)

class SpriteCharacter:
    """Eigene Figur aus PNG-Bildern im "PNGtuber"-Stil.

    Ordner mit character.json und Bildern, z.B.:
        {"type": "sprite", "name": "Rezo", "color": "#3d8bff",
         "images": {"idle": "idle.png", "talk": "talk.png", "blink": "blink.png", "talk_blink": "talk_blink.png"},
         "height": 640, "offset_y": 120}
    Die Bilder sollten gleich groß sein (transparenter Hintergrund). Unterkante = Unterkante der Figur.
    """

    def __init__(self, style: dict):
        self.s = style
        self.scale = float(style.get("scale", 1.0))
        base = Path(style.get("_dir", "."))
        self.images: dict[str, skia.Image] = {}
        for key, name in (style.get("images") or {}).items():
            p = base / name
            if p.exists():
                img = skia.Image.open(str(p))
                if img is not None:
                    self.images[key] = img.withDefaultMipmaps()
        if "idle" not in self.images and self.images:
            self.images["idle"] = next(iter(self.images.values()))
        self.height = float(style.get("height", 640))
        self.offset_y = float(style.get("offset_y", 140))

    def head_anchor(self) -> tuple[float, float]:
        return 0.0, (-self.height * 0.72 + self.offset_y) * self.scale

    def _pick(self, st: CharState) -> skia.Image | None:
        talking = st.talk > 0.22 or st.laugh > 0.4
        blink = st.blink > 0.5
        for key in (["talk_blink", "talk"] if talking and blink else ["talk"] if talking else
                    ["blink"] if blink else []) + ["idle"]:
            if key in self.images:
                return self.images[key]
        return None

    def draw_body(self, c: skia.Canvas, st: CharState) -> None:
        img = self._pick(st)
        if img is None:
            return
        k = self.height / img.height()
        w = img.width() * k
        bounce = -10 * min(1.0, st.talk) if st.speaking else 0.0
        squash = 1.0 + 0.015 * min(1.0, st.talk)
        c.save()
        c.scale(self.scale, self.scale)
        c.translate(st.head_dx * 0.5, self.offset_y + st.body_dy + bounce)
        c.rotate(st.head_rot * 0.5, 0, 0)
        c.scale(1.0 / squash, squash)
        c.drawImageRect(img, skia.Rect.MakeXYWH(-w / 2, -self.height, w, self.height),
                        skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kLinear))
        c.restore()

    def draw_front(self, c: skia.Canvas, st: CharState, inner: int) -> None:
        return
