import numpy as np

from podcast_animator import media
from podcast_animator.render.characters import get_character
from podcast_animator.render.scene import H, W, clamp_camera, plan_shots
from podcast_animator.render.video import ClipRenderer, RenderOptions, render_clip, seats_from_mapping


def _seats():
    chars = {c: get_character(c) for c in ("rezo", "julien")}
    return seats_from_mapping([{"spk": 0, "char": "rezo"}, {"spk": 1, "char": "julien"}], chars)


def test_shot_plan_covers_clip():
    segs = [(0.0, 4.0, 0), (4.2, 5.0, 1), (5.1, 12.0, 1), (12.2, 13.0, 0), (13.1, 25.0, 0)]
    shots = plan_shots(segs, 26.0, 2)
    assert shots[0].start == 0.0 and shots[-1].end >= 26.0
    for a, b in zip(shots, shots[1:]):
        assert abs(a.end - b.start) < 1e-6
        assert b.end - b.start >= 0.9
    assert {s.kind for s in shots} >= {"wide", "close"}


def test_camera_clamp_stays_inside():
    cx, cy, z = clamp_camera(0, 0, 1.8)
    assert cx - W / 2 / z >= 0 and cy - H / 2 / z >= 0


def test_frame_renders_both_layouts(dialog_words):
    env = np.full(30 * 22, 0.7, dtype=np.float32)
    for layout in ("studio", "split"):
        r = ClipRenderer(dialog_words, env, 22.0, _seats(), RenderOptions(layout=layout, title="Test"))
        img = r.snapshot(60)
        assert img.width() == W and img.height() == H
        px = img.toarray()
        assert px[..., :3].std() > 20  # nicht leer


def test_render_clip_end_to_end(tmp_path, dialog_words, speech_wav):
    out = tmp_path / "clip.mp4"
    res = render_clip(speech_wav, speech_wav, dialog_words, 2.0, 8.0, _seats(), out,
                      RenderOptions(title="Ende-zu-Ende"), workers=1)
    info = media.probe(out)
    assert info["has_video"] and info["has_audio"]
    assert abs(info["duration"] - 6.0) < 0.2
    assert (tmp_path / "clip.srt").exists() and (tmp_path / "clip.jpg").exists()
    assert res["duration"] == 6.0


def test_render_clip_parallel(tmp_path, dialog_words, speech_wav):
    out = tmp_path / "par.mp4"
    render_clip(speech_wav, speech_wav, dialog_words, 0.0, 7.0, _seats(), out, RenderOptions(), workers=2)
    info = media.probe(out)
    assert abs(info["duration"] - 7.0) < 0.2
