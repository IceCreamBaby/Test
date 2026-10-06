import numpy as np

from conftest import make_words
from podcast_animator import highlights, media


def _long_podcast():
    turns, t = [], 0.0
    boring = "Und dann haben wir halt noch über verschiedene Sachen gesprochen die man so macht."
    funny = "Alter das ist so krass! Hahaha nein wirklich? Doch ernsthaft!"
    for i in range(60):
        text = funny if 20 <= i < 26 else boring
        spk = i % 2 if 20 <= i < 26 else (0 if i % 6 else 1)
        dur = 3.0 if text is funny else 6.0
        turns.append((spk, text, t, t + dur))
        t += dur + 0.3
    return make_words(turns)


def test_local_finder_prefers_lively_part():
    words = _long_podcast()
    env = np.ones(int(words[-1]["e"] * 10) + 1, dtype=np.float32)
    res = highlights.find_local(words, env, count=3, min_len=15, max_len=40)
    assert res, "keine Clips gefunden"
    best = res[0]
    funny_start = next(w["s"] for w in words if w["w"] == "Alter")
    assert best.start <= funny_start + 25 and best.end >= funny_start
    for a in res:
        assert 15 <= a.end - a.start <= 40
        for b in res:
            if a is not b:
                assert min(a.end, b.end) - max(a.start, b.start) < 0.2 * min(a.end - a.start, b.end - b.start)


def test_snap_to_words(dialog_words):
    s, e = highlights.snap_to_words(dialog_words, 3.4, 10.1)
    assert abs(s - (3.3 - 0.15)) < 0.01
    assert e > 10.0


def test_transcript_for_llm(dialog_words):
    txt = highlights.transcript_for_llm(dialog_words, {0: "Rezo", 1: "Julien"})
    assert txt.splitlines()[0].startswith("[0.0] Rezo: Alter")


def test_parse_and_format_ts():
    assert media.parse_ts("1:02:03.5") == 3723.5
    assert media.parse_ts("62:03") == 3723
    assert media.parse_ts("") is None
    assert media.format_ts(3723.5) == "1:02:03.5"
