"""Findet die besten Stellen eines Podcasts für Shorts.

Zwei Wege:
* lokal (immer verfügbar): Heuristik aus Sprecherwechseln, Lachern, Ausrufen, Lautstärke, Schlüsselwörtern
* mit Claude (optional, API-Key nötig): versteht Inhalt, Pointen und Kontext und schreibt Titel
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

import numpy as np

from .media import format_ts
from .transcript import LAUGH, sentences, utterances

log = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-opus-5-5"

KEYWORDS = {
    "krass", "alter", "digga", "diggah", "boah", "wtf", "geil", "lustig", "witzig", "ernsthaft", "wirklich",
    "safe", "junge", "bruder", "bro", "unfassbar", "unglaublich", "peinlich", "verrückt", "wahnsinn", "absurd",
    "niemals", "hass", "liebe", "angst", "schlimm", "skandal", "hä", "was", "warte", "stopp", "nein", "doch",
    "genau", "eskaliert", "cringe", "legendär", "ehrlich", "geheimnis", "story", "passiert", "fuck", "scheiße",
    "mega", "insane", "lol", "haha",
}
AD_WORDS = {"werbung", "sponsor", "sponsored", "rabattcode", "rabatt", "gutschein", "beschreibung", "affiliate",
            "präsentiert", "partner", "abonnieren", "abonniert", "patreon", "merch", "tickets", "code"}
WEAK_START = {"und", "aber", "also", "weil", "denn", "oder", "sondern", "dass", "da", "dann", "ja", "ähm", "äh", "hm"}


@dataclass
class Suggestion:
    start: float
    end: float
    score: float
    title: str
    reason: str
    source: str = "lokal"

    def to_dict(self) -> dict:
        return {"start": round(self.start, 2), "end": round(self.end, 2), "score": round(self.score, 2),
                "title": self.title, "reason": self.reason, "source": self.source}


def _norm(word: str) -> str:
    return re.sub(r"[^\wäöüß]", "", word.lower())


# --------------------------------------------------------------------------- lokale Heuristik

def find_local(words: list[dict], env10: np.ndarray | None, count: int = 5, min_len: float = 20.0,
               max_len: float = 55.0, speaker_names: dict[int, str] | None = None,
               avoid_speakers: set[int] | None = None) -> list[Suggestion]:
    if not words:
        return []
    sents = sentences(words)
    if not sents:
        return []
    # Präfixsummen pro Wort
    n = len(words)
    turn = np.zeros(n + 1)
    laugh = np.zeros(n + 1)
    excl = np.zeros(n + 1)
    kw = np.zeros(n + 1)
    ad = np.zeros(n + 1)
    foreign = np.zeros(n + 1)
    avoid = avoid_speakers or set()
    for i, w in enumerate(words):
        foreign[i + 1] = foreign[i] + (1 if w.get("spk") in avoid else 0)
        t = _norm(w["w"])
        turn[i + 1] = turn[i] + (1 if i > 0 and w.get("spk") != words[i - 1].get("spk") else 0)
        laugh[i + 1] = laugh[i] + (1 if LAUGH.search(w["w"]) else 0)
        excl[i + 1] = excl[i] + (1 if w["w"].endswith(("!", "?")) else 0)
        kw[i + 1] = kw[i] + (1 if t in KEYWORDS else 0)
        ad[i + 1] = ad[i] + (1 if t in AD_WORDS else 0)
    index_of = {id(w): i for i, w in enumerate(words)}
    spks = sorted({w.get("spk", 0) for w in words})
    talk_cs = {k: np.concatenate([[0.0], np.cumsum([(w["e"] - w["s"]) if w.get("spk", 0) == k else 0.0
                                                    for w in words])]) for k in spks}
    env_cs = None
    if env10 is not None and len(env10):
        env_cs = np.concatenate([[0.0], np.cumsum(env10)])
        env_cs2 = np.concatenate([[0.0], np.cumsum(env10.astype(np.float64) ** 2)])
        g_mean = float(np.mean(env10))
        g_std = float(np.std(env10)) + 1e-6

    def env_stats(s: float, e: float) -> tuple[float, float]:
        if env_cs is None:
            return 0.0, 0.0
        a, b = int(s * 10), max(int(s * 10) + 1, int(e * 10))
        b = min(b, len(env10))
        a = min(a, b - 1)
        cnt = b - a
        mean = (env_cs[b] - env_cs[a]) / cnt
        var = max(0.0, (env_cs2[b] - env_cs2[a]) / cnt - mean ** 2)
        return (mean - g_mean) / g_std, math.sqrt(var) / g_std

    cands: list[tuple[float, float, float, int, int, str]] = []
    for i, first in enumerate(sents):
        s = first.start
        first_word = _norm(first.words[0]["w"])
        for j in range(i, len(sents)):
            e = sents[j].end
            dur = e - s
            if dur > max_len:
                break
            if dur < min_len:
                continue
            a = index_of[id(first.words[0])]
            b = index_of[id(sents[j].words[-1])] + 1
            nwords = b - a
            minutes = dur / 60
            turns = (turn[b] - turn[a + 1]) if b > a + 1 else 0
            f_turns = min(1.0, turns / minutes / 9.0)
            f_laugh = min(1.5, 0.5 * (laugh[b] - laugh[a]))
            f_excl = min(1.0, (excl[b] - excl[a]) / minutes / 10.0)
            f_kw = min(1.0, (kw[b] - kw[a]) / minutes / 8.0)
            f_ad = (ad[b] - ad[a])
            rate = nwords / dur
            f_rate = min(1.0, max(0.0, (rate - 1.6) / 1.4))
            loud, dyn = env_stats(s, e)
            # Sprecher-Balance: Dialog ist spannender als Monolog
            talk = [talk_cs[k][b] - talk_cs[k][a] for k in spks]
            tot = sum(talk) or 1.0
            minority = 1.0 - max(talk) / tot if len(spks) > 1 else 0.0
            f_bal = min(1.0, minority / 0.3)
            f_len = math.exp(-((dur - 38.0) / 18.0) ** 2)
            ends_clean = 1.0 if re.search(r"[.!?…]$", sents[j].words[-1]["w"]) else 0.0
            score = (1.4 * f_turns + 1.2 * f_laugh + 0.8 * f_excl + 0.9 * f_kw + 0.5 * f_rate + 0.8 * f_bal
                     + 0.35 * max(-1.0, min(1.5, loud)) + 0.35 * min(1.5, dyn) + 0.4 * f_len + 0.3 * ends_clean
                     - (0.6 if first_word in WEAK_START else 0.0) - 1.2 * min(3.0, f_ad)
                     - 6.0 * (foreign[b] - foreign[a]) / max(1, nwords))
            reason = []
            if f_laugh > 0:
                reason.append(f"{int(laugh[b] - laugh[a])}× Lachen")
            if f_turns > 0.5:
                reason.append("schnelles Hin und Her")
            if f_excl > 0.5:
                reason.append("viele Ausrufe/Fragen")
            if f_kw > 0.4:
                reason.append("emotionale Wörter")
            if loud > 0.5:
                reason.append("laut/energiegeladen")
            cands.append((score, s, e, a, b, ", ".join(reason) or "solider Dialog"))
    cands.sort(key=lambda c: -c[0])
    picked: list[tuple[float, float, float, int, int, str]] = []
    for c in cands:
        if all(min(c[2], p[2]) - max(c[1], p[1]) < 0.15 * min(c[2] - c[1], p[2] - p[1]) for p in picked):
            picked.append(c)
        if len(picked) >= count:
            break
    out = []
    for score, s, e, a, b, reason in picked:
        out.append(Suggestion(s, e, score, _local_title(words[a:b]), reason, "lokal"))
    return out


def _local_title(ws: list[dict]) -> str:
    """Titel ohne KI: das prägnanteste vollständige Zitat aus dem ersten Teil des Clips („…“)."""
    if not ws:
        return ""
    # Sätze nur nach Satzzeichen/Pausen trennen (Sprecherwechsel ignorieren – die können falsch sein)
    sents: list[list[dict]] = [[]]
    for w in ws:
        if sents[-1] and w["s"] - sents[-1][-1]["e"] > 1.0:
            sents.append([])
        sents[-1].append(w)
        if re.search(r"[.!?…]$", w["w"]):
            sents.append([])
    t0, t1 = ws[0]["s"], ws[-1]["e"]
    best, best_score = "", -1e9
    for sent in sents:
        if not sent or sent[0]["s"] > t0 + 0.7 * (t1 - t0):
            continue
        text = " ".join(w["w"] for w in sent).strip().rstrip(",;:")
        if len(sent) < 4 or not 22 <= len(text) <= 85:
            continue
        toks = [_norm(w["w"]) for w in sent]
        score = -abs(len(text) - 45) / 25.0
        score += 2.0 if text.endswith(("?", "!")) else 0.0
        score += sum(1.0 for t in toks if t in KEYWORDS)
        score += 0.5 if any(t in ("du", "ich", "wir", "dich", "mir") for t in toks) else 0.0
        score -= 1.0 if toks[0] in WEAK_START else 0.0
        score -= 3.0 * sum(1 for t in toks if t in AD_WORDS)
        if score > best_score:
            best, best_score = text, score
    if not best:
        return ""
    best = best[:1].upper() + best[1:]
    return f"„{best}“"


# --------------------------------------------------------------------------- Claude

def transcript_for_llm(words: list[dict], speaker_names: dict[int, str]) -> str:
    lines = []
    for g in utterances(words, max_gap=1.2):
        name = speaker_names.get(g.spk, f"Sprecher {g.spk + 1}")
        lines.append(f"[{g.start:.1f}] {name}: {g.text}")
    return "\n".join(lines)


def find_with_claude(words: list[dict], speaker_names: dict[int, str], count: int = 5, min_len: float = 20.0,
                     max_len: float = 55.0, api_key: str | None = None, context: str = "") -> list[Suggestion]:
    import anthropic
    from pydantic import BaseModel, Field

    class Clip(BaseModel):
        start: float = Field(description="Startzeit in Sekunden (aus den Zeitstempeln)")
        end: float = Field(description="Endzeit in Sekunden")
        title: str = Field(description="Kurzer, packender Shorts-Titel auf Deutsch (max. 60 Zeichen)")
        reason: str = Field(description="Warum funktioniert dieser Ausschnitt als Short? (1 Satz)")
        score: int = Field(description="Viral-Potenzial 1-10")

    class ClipList(BaseModel):
        clips: list[Clip]

    transcript = transcript_for_llm(words, speaker_names)
    system = (
        "Du bist ein erfahrener Editor für deutschsprachige YouTube Shorts und schneidest Podcast-Highlights. "
        "Du bekommst ein Transkript mit Zeitstempeln in Sekunden ([Sekunde] Sprecher: Text). "
        "Wähle Ausschnitte, die ohne weiteren Kontext funktionieren: starker Einstieg in den ersten 2 Sekunden, "
        "abgeschlossener Gedanke oder Pointe am Ende, lustig, überraschend, kontrovers oder emotional. "
        "Meide Werbung, Sponsoren, Begrüßungen, Verabschiedungen und Gerede ohne Pointe. "
        "Ein Ausschnitt beginnt an einem Satzanfang und endet direkt nach der Pointe. "
        "Titel: neugierig machend, ehrlich (kein erfundener Inhalt), ohne Hashtags und ohne Emojis."
    )
    user = (
        f"{('Kontext zum Podcast: ' + context + chr(10) + chr(10)) if context else ''}"
        f"Finde die {count} besten Ausschnitte mit einer Länge von {int(min_len)} bis {int(max_len)} Sekunden. "
        f"Die Ausschnitte dürfen sich nicht überschneiden. Sortiere nach Viral-Potenzial (beste zuerst).\n\n"
        f"<transkript>\n{transcript}\n</transkript>"
    )
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=ClipList,
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude hat die Anfrage abgelehnt.")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError("Claude hat keine gültige Antwort geliefert.")
    out: list[Suggestion] = []
    for c in response.parsed_output.clips:
        s, e = snap_to_words(words, c.start, c.end)
        if e - s < 5:
            continue
        if any(min(e, o.end) - max(s, o.start) > 0.2 * min(e - s, o.end - o.start) for o in out):
            continue
        out.append(Suggestion(s, e, float(c.score), c.title.strip()[:90], c.reason.strip(), "Claude"))
    return out[:count]


def snap_to_words(words: list[dict], start: float, end: float) -> tuple[float, float]:
    """Rastet Start/Ende auf Wortgrenzen ein (mit etwas Luft, damit kein Wort abgeschnitten wird)."""
    if not words:
        return start, end
    starts = [w for w in words if w["e"] > start]
    s = starts[0]["s"] if starts else start
    ends = [w for w in words if w["s"] < end]
    e = ends[-1]["e"] if ends else end
    return max(0.0, s - 0.15), e + 0.35


def find_clips(words: list[dict], env10: np.ndarray | None, speaker_names: dict[int, str], count: int = 5,
               min_len: float = 20.0, max_len: float = 55.0, use_claude: bool = False, api_key: str | None = None,
               context: str = "", progress=None) -> tuple[list[Suggestion], str]:
    """Gibt (Vorschläge, Hinweistext) zurück. Fällt bei Problemen mit Claude auf die lokale Suche zurück."""
    note = ""
    if use_claude:
        try:
            if progress:
                progress(0.2, "Claude sucht die besten Stellen …")
            res = find_with_claude(words, speaker_names, count, min_len, max_len, api_key, context)
            if res:
                return res, "Clips von Claude ausgewählt."
            note = "Claude hat keine Clips geliefert – lokale Suche verwendet."
        except Exception as exc:  # Netzwerk, Key, Limits …
            log.warning("Claude-Clipsuche fehlgeschlagen: %s", exc)
            note = f"Claude nicht verfügbar ({type(exc).__name__}: {str(exc)[:160]}) – lokale Suche verwendet."
    if progress:
        progress(0.5, "Suche lustige und spannende Stellen …")
    return find_local(words, env10, count, min_len, max_len, speaker_names, avoid_speakers), note


def describe(s: Suggestion) -> str:
    return f"{format_ts(s.start)}–{format_ts(s.end)} ({s.end - s.start:.0f}s) Score {s.score:.1f}: {s.title}"
