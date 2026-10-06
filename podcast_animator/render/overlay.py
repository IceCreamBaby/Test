"""Overlays im Shorts-Stil: Untertitel mit Wort-Hervorhebung, Titel, Namensschild, Wasserzeichen."""
from __future__ import annotations

from dataclasses import dataclass

import skia

from ..transcript import Group, subtitle_chunks
from .draw import blurred, color, ease, ease_out_back, fill, font, rrect, stroke

SUB_FONT = "Montserrat-Black.ttf"
TITLE_FONT = "Montserrat-Black.ttf"


@dataclass
class SubtitleStyle:
    size: float = 82
    y: float = 1430
    uppercase: bool = False
    max_words: int = 3
    show_names: bool = True
    text_color: str = "#ffffff"
    outline: float = 13
    max_width: float = 930


class Subtitles:
    def __init__(self, words: list[dict], speaker_colors: dict[int, str], speaker_names: dict[int, str],
                 style: SubtitleStyle):
        self.style = style
        self.chunks: list[Group] = subtitle_chunks(words, max_words=style.max_words,
                                                   max_chars=18 if style.max_words <= 3 else 28)
        self.colors = speaker_colors
        self.names = speaker_names
        self.font = font(SUB_FONT, style.size)
        self.name_font = font("Montserrat-ExtraBold.ttf", 34)

    def _chunk_at(self, t: float) -> tuple[int, Group | None]:
        for i, ch in enumerate(self.chunks):
            nxt = self.chunks[i + 1].start if i + 1 < len(self.chunks) else ch.end + 0.8
            if ch.start - 0.05 <= t < min(nxt, ch.end + 0.8):
                return i, ch
        return -1, None

    def _text(self, w: str) -> str:
        return w.upper() if self.style.uppercase else w

    def draw(self, c: skia.Canvas, t: float, cx: float = 540, y: float | None = None) -> None:
        idx, ch = self._chunk_at(t)
        if ch is None:
            return
        st = self.style
        y = st.y if y is None else y
        hl = self.colors.get(ch.spk, "#ffd400")
        # Zeilenumbruch
        space = self.font.measureText(" ") + st.size * 0.12
        lines: list[list[tuple[dict, float]]] = [[]]
        width = 0.0
        for w in ch.words:
            ww = self.font.measureText(self._text(w["w"]))
            if lines[-1] and width + space + ww > st.max_width:
                lines.append([])
                width = 0.0
            width += (space if lines[-1] else 0) + ww
            lines[-1].append((w, ww))
        # Pop-Animation beim Erscheinen
        p = (t - ch.start + 0.05) / 0.16
        scale = 0.78 + 0.22 * ease_out_back(p, 2.2)
        alpha = min(1.0, max(0.0, p * 2))
        line_h = st.size * 1.12
        total_h = line_h * len(lines)
        c.save()
        c.translate(cx, y)
        c.scale(scale, scale)
        # Namensschild
        if st.show_names and self.names.get(ch.spk):
            self._name_tag(c, self.names[ch.spk], hl, -total_h / 2 - st.size * 0.55, alpha)
        for li, line in enumerate(lines):
            lw = sum(ww for _, ww in line) + space * (len(line) - 1)
            x = -lw / 2
            base = -total_h / 2 + line_h * (li + 0.8)
            for w, ww in line:
                text = self._text(w["w"])
                active = w["s"] - 0.02 <= t < max(w["e"], w["s"] + 0.12)
                k = 1.0
                if active:
                    k = 1.0 + 0.07 * ease((t - w["s"]) / 0.08)
                c.save()
                c.translate(x + ww / 2, base - st.size * 0.35)
                c.scale(k, k)
                c.translate(-ww / 2, st.size * 0.35)
                shadow = blurred(fill("#000000", 0.55 * alpha), 6)
                c.drawString(text, 4, 7, self.font, shadow)
                c.drawString(text, 0, 0, self.font, stroke("#0b0b0f", st.outline, alpha))
                col = hl if active else st.text_color
                c.drawString(text, 0, 0, self.font, fill(col, alpha))
                c.restore()
                x += ww + space
        c.restore()

    def _name_tag(self, c: skia.Canvas, name: str, col: str, y: float, alpha: float) -> None:
        text = name.upper()
        w = self.name_font.measureText(text)
        r = rrect(-w / 2 - 18, y - 30, w + 36, 44, 22)
        c.drawPath(r, fill(col, 0.95 * alpha))
        c.drawPath(r, stroke("#0b0b0f", 4, alpha))
        c.drawString(text, -w / 2, y + 3, self.name_font, fill("#0b0b0f", alpha))


def _greedy(words: list[str], f: skia.Font, max_w: float) -> list[str]:
    lines, cur = [], ""
    for word in words:
        cand = f"{cur} {word}".strip()
        if cur and f.measureText(cand) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def wrap(text: str, f: skia.Font, max_w: float) -> list[str]:
    """Zeilenumbruch mit möglichst gleich langen Zeilen."""
    words = text.split()
    lines = _greedy(words, f, max_w)
    if len(lines) < 2:
        return lines
    lo = max(f.measureText(w) for w in words)
    hi = max_w
    for _ in range(14):
        mid = (lo + hi) / 2
        if len(_greedy(words, f, mid)) <= len(lines):
            hi = mid
        else:
            lo = mid
    return _greedy(words, f, hi)


def draw_title(c: skia.Canvas, title: str, t: float, y: float = 250, accent: str = "#ffd400",
               small: bool = False) -> None:
    if not title:
        return
    size = 44 if small else 58
    f = font(TITLE_FONT, size)
    lines = wrap(title, f, 860)[:3]
    if len(lines) == 3 or any(f.measureText(ln) > 860 for ln in lines):
        f = font(TITLE_FONT, size * 0.83)
        lines = wrap(title, f, 880)[:3]
    lh = f.getSize() * 1.18
    box_w = max(f.measureText(ln) for ln in lines) + 70
    box_h = lh * len(lines) + 44
    p = ease_out_back(t / 0.35, 1.6)
    c.save()
    c.translate(540, y)
    c.scale(p, p)
    c.rotate(-1.5)
    r = rrect(-box_w / 2, -box_h / 2, box_w, box_h, 26)
    c.drawPath(rrect(-box_w / 2 + 8, -box_h / 2 + 10, box_w, box_h, 26), fill("#000000", 0.45))
    c.drawPath(r, fill("#ffffff"))
    c.drawPath(r, stroke("#0b0b0f", 6))
    c.drawRect(skia.Rect.MakeXYWH(-box_w / 2 + 26, box_h / 2 - 10, box_w - 52, 6), fill(accent))
    for i, line in enumerate(lines):
        lw = f.measureText(line)
        c.drawString(line, -lw / 2, -box_h / 2 + 22 + lh * (i + 0.78), f, fill("#0b0b0f"))
    c.restore()


def draw_watermark(c: skia.Canvas, text: str, y: float = 1610) -> None:
    if not text:
        return
    f = font("Montserrat-ExtraBold.ttf", 32)
    w = f.measureText(text)
    c.drawString(text, 540 - w / 2 + 2, y + 2, f, fill("#000000", 0.45))
    c.drawString(text, 540 - w / 2, y, f, fill("#ffffff", 0.75))


def draw_label(c: skia.Canvas, text: str, x: float, y: float, col: str, size: float = 30) -> None:
    f = font("Montserrat-ExtraBold.ttf", size)
    w = f.measureText(text.upper())
    r = rrect(x - w / 2 - 16, y - size, w + 32, size * 1.45, size * 0.7)
    c.drawPath(r, fill(col, 0.92))
    c.drawString(text.upper(), x - w / 2, y + size * 0.12, f, fill("#0b0b0f"))


def dim(c: skia.Canvas, rect: skia.Rect, amount: float) -> None:
    if amount > 0.01:
        c.drawRect(rect, skia.Paint(Color=color("#000000", amount)))
