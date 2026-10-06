"""Wörter, Sprecher, Sätze und Untertitel-Häppchen."""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

SENTENCE_END = re.compile(r"[.!?…]+[\"'»“”)]*$")
LAUGH = re.compile(r"\b(ha(ha)+|he(he)+|hi(hi)+|lach\w*|\*lacht\*|\(lacht\)|\[lachen\])\b", re.IGNORECASE)


def assign_speakers(words: list[dict], segs: list[dict]) -> None:
    """Weist jedem Wort den Sprecher mit der größten zeitlichen Überlappung zu (in place)."""
    if not segs:
        for w in words:
            w["spk"] = 0
        return
    segs = sorted(segs, key=lambda s: s["s"])
    starts = [s["s"] for s in segs]
    for w in words:
        mid = (w["s"] + w["e"]) / 2
        i = bisect.bisect_right(starts, w["e"])
        best, best_ov = None, 0.0
        for j in range(max(0, i - 8), min(len(segs), i + 1)):
            s = segs[j]
            ov = min(w["e"], s["e"]) - max(w["s"], s["s"])
            if ov > best_ov:
                best, best_ov = s, ov
        if best is None:  # Wort liegt in keiner Sprecher-Region -> nächste Region
            j = min(range(max(0, i - 2), min(len(segs), i + 2)),
                    key=lambda k: min(abs(mid - segs[k]["s"]), abs(mid - segs[k]["e"])))
            best = segs[j]
        w["spk"] = int(best["spk"])


def words_in_range(words: list[dict], start: float, end: float) -> list[dict]:
    """Wörter, deren Mitte im Bereich liegt (als Kopie)."""
    return [dict(w) for w in words if start <= (w["s"] + w["e"]) / 2 < end]


def shift_words(words: list[dict], offset: float) -> list[dict]:
    out = []
    for w in words:
        nw = dict(w)
        nw["s"] = round(w["s"] - offset, 3)
        nw["e"] = round(w["e"] - offset, 3)
        out.append(nw)
    return out


@dataclass
class Group:
    spk: int
    words: list[dict] = field(default_factory=list)

    @property
    def start(self) -> float:
        return self.words[0]["s"]

    @property
    def end(self) -> float:
        return self.words[-1]["e"]

    @property
    def text(self) -> str:
        return join_words(self.words)

    def to_dict(self) -> dict:
        return {"spk": self.spk, "s": self.start, "e": self.end, "text": self.text}


def join_words(words: list[dict]) -> str:
    return " ".join(w["w"] for w in words).strip()


def utterances(words: list[dict], max_gap: float = 1.5) -> list[Group]:
    """Zusammenhängende Redebeiträge eines Sprechers."""
    groups: list[Group] = []
    for w in words:
        if groups and groups[-1].spk == w.get("spk", 0) and w["s"] - groups[-1].end <= max_gap:
            groups[-1].words.append(w)
        else:
            groups.append(Group(w.get("spk", 0), [w]))
    return groups


def sentences(words: list[dict], max_gap: float = 1.0) -> list[Group]:
    """Grobe Sätze: Satzzeichen, Sprecherwechsel oder lange Pausen trennen."""
    groups: list[Group] = []
    for w in words:
        new = (not groups or groups[-1].spk != w.get("spk", 0)
               or w["s"] - groups[-1].end > max_gap
               or SENTENCE_END.search(groups[-1].words[-1]["w"]) is not None)
        if new:
            groups.append(Group(w.get("spk", 0), [w]))
        else:
            groups[-1].words.append(w)
    return groups


def subtitle_chunks(words: list[dict], max_words: int = 3, max_chars: int = 18,
                    max_gap: float = 0.45, split_after: str = ".!?…,;:") -> list[Group]:
    """Kurze Untertitel-Häppchen im Shorts-Stil (wenige Wörter, Wechsel bei Sprecher/Pause/Satzende)."""
    chunks: list[Group] = []
    for w in words:
        cur = chunks[-1] if chunks else None
        if cur is not None:
            last = cur.words[-1]
            length = len(cur.text) + 1 + len(w["w"])
            split = (cur.spk != w.get("spk", 0) or w["s"] - last["e"] > max_gap
                     or len(cur.words) >= max_words or length > max_chars
                     or (bool(split_after) and last["w"][-1:] in split_after))
            if not split:
                cur.words.append(w)
                continue
        chunks.append(Group(w.get("spk", 0), [w]))
    return chunks


def is_laugh(word: str) -> bool:
    return bool(LAUGH.search(word))


def retime_text(old_words: list[dict], new_text: str, spk: int) -> list[dict]:
    """Verteilt die Zeitspanne einer bearbeiteten Zeile proportional zur Länge auf die neuen Wörter."""
    tokens = new_text.split()
    if not tokens or not old_words:
        return []
    if [w["w"] for w in old_words] == tokens:
        return [dict(w, spk=spk) for w in old_words]
    start, end = old_words[0]["s"], old_words[-1]["e"]
    weights = [len(t) + 2 for t in tokens]
    total = float(sum(weights))
    out, t = [], start
    for tok, wgt in zip(tokens, weights):
        dur = (end - start) * wgt / total
        out.append({"w": tok, "s": round(t, 3), "e": round(t + dur * 0.92, 3), "p": 1.0, "spk": spk})
        t += dur
    return out


def to_srt(words: list[dict], speaker_names: dict[int, str] | None = None) -> str:
    """SRT-Datei aus Wörtern (etwas längere Zeilen als die Video-Untertitel)."""
    def ts(sec: float) -> str:
        ms = int(round(max(0.0, sec) * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    lines = []
    chunks = subtitle_chunks(words, max_words=10, max_chars=50, max_gap=0.8, split_after=".!?…")
    for i, g in enumerate(chunks, 1):
        name = (speaker_names or {}).get(g.spk)
        text = f"{name}: {g.text}" if name else g.text
        # mindestens ~0,8 s sichtbar, aber nicht über den nächsten Eintrag hinaus
        nxt = chunks[i].start if i < len(chunks) else g.end + 1.0
        end = min(max(g.end, g.start + 0.8), nxt - 0.02) if nxt > g.end else g.end
        lines.append(f"{i}\n{ts(g.start)} --> {ts(end)}\n{text}\n")
    return "\n".join(lines)
