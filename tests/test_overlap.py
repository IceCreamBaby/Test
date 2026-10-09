"""Gleichzeitiges Reden: eingezeichnete Bereiche, Totale bei Überlappung, doppelte Untertitel, Extra-Personen."""
import numpy as np

from conftest import make_words
from podcast_animator.render.animate import Animator
from podcast_animator.render.characters import get_character
from podcast_animator.render.overlay import SubtitleStyle, Subtitles
from podcast_animator.render.scene import overlap_intervals, plan_shots
from podcast_animator.render.video import ClipRenderer, RenderOptions, local_regions, seats_from_mapping


def test_region_makes_listener_talk():
    words = make_words([(0, "Ich erzähle hier eine lange Geschichte über meinen Hund", 0, 6)])
    env = np.full(30 * 6, 0.8, dtype=np.float32)
    plain = Animator(words, env, 30, 180, {0: 0, 1: 1}, [1, -1])
    with_region = Animator(words, env, 30, 180, {0: 0, 1: 1}, [1, -1], regions=[{"spk": 1, "s": 2.0, "e": 4.0}])
    f = 90  # 3 s
    assert plain.talk[1, f] < 0.05
    assert with_region.talk[1, f] > 0.3 and with_region.talk[0, f] > 0.3  # beide Münder bewegen sich
    assert with_region.state(1, f).viseme != "rest"
    assert with_region.talk[1, 150] < 0.2  # nach dem Bereich wieder zu


def test_overlap_gives_wide_shot():
    segs = [(0.0, 10.0, 0), (4.0, 6.5, 1), (10.5, 20.0, 1)]
    assert overlap_intervals(segs) == [(4.0, 6.5)]
    shots = plan_shots(segs, 20.0, 2)
    at5 = next(s for s in shots if s.start <= 5.0 < s.end)
    assert at5.kind == "wide"


def test_overlapping_subtitles_show_both():
    words = make_words([(0, "Das ist eine sehr lange Erklärung", 0, 4), (1, "Ja genau", 1.0, 2.0),
                        (1, "Und dann?", 5.0, 6.0)])
    subs = Subtitles(words, {0: "#38a3ff", 1: "#ffb020"}, {0: "Rezo", 1: "Julien"}, SubtitleStyle())
    assert {ch.spk for ch in subs.chunks_at(1.5)} == {0, 1}
    # nacheinander gesprochen -> nie zwei gleichzeitig
    assert len(subs.chunks_at(5.5)) == 1 and subs.chunks_at(5.5)[0].spk == 1


def test_extra_person_gets_a_seat_and_talks():
    chars = {c: get_character(c) for c in ("rezo", "julien", "gast")}
    seats = seats_from_mapping([{"spk": 0, "char": "rezo"}, {"spk": 1, "char": "julien"},
                                {"spk": 1000, "char": "gast"}], chars)
    assert [s.style["id"] for s in seats] == ["rezo", "julien", "gast"]
    words = make_words([(0, "Hallo zusammen wie geht es euch", 0, 3)])
    regions = local_regions([{"spk": 1000, "s": 11.0, "e": 12.5}], 10.0, 14.0)
    assert regions == [{"spk": 1000, "s": 1.0, "e": 2.5}]
    r = ClipRenderer(words, np.full(120, 0.8, np.float32), 4.0, seats, RenderOptions(), regions)
    assert len(r.slots) == 3
    assert r.anim.talk[2, 50] > 0.3  # Gast (Platz 3) redet in seinem Bereich
    assert r.snapshot(50).width() == 1080
