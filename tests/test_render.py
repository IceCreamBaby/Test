import numpy as np
import skia

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
    # Farben im fertigen Video == Farben der Vorschau (fängt vertauschte Farbkanäle ab)
    raw = media.run_ffmpeg(["-i", str(out), "-vf", "select=eq(n\\,60)", "-frames:v", "1",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]).stdout
    frame = np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 3).astype(int)
    from podcast_animator.render.video import prepare
    preview = prepare(speech_wav, dialog_words, 2.0, 8.0, _seats(), RenderOptions(title="Ende-zu-Ende")) \
        .snapshot(60).toarray(colorType=skia.kRGBA_8888_ColorType)[..., :3].astype(int)
    assert np.abs(frame - preview).mean() < 6.0
    assert abs(frame[..., 0].mean() - preview[..., 0].mean()) < 4.0  # Rotkanal
    assert abs(frame[..., 2].mean() - preview[..., 2].mean()) < 4.0  # Blaukanal


def test_render_clip_parallel(tmp_path, dialog_words, speech_wav):
    out = tmp_path / "par.mp4"
    render_clip(speech_wav, speech_wav, dialog_words, 0.0, 7.0, _seats(), out, RenderOptions(), workers=2)
    info = media.probe(out)
    assert abs(info["duration"] - 7.0) < 0.2


def test_video_frames_have_same_colors_as_preview(dialog_words):
    """Regression: unter Windows ist skias Standardformat BGRA -> im Video waren Gesichter blau."""
    import skia

    from podcast_animator.render.video import FRAME_PIX_FMT, frame_surface

    buf = np.zeros((H, W, 4), dtype=np.uint8)
    surf = frame_surface(buf)
    assert surf.imageInfo().colorType() == skia.kRGBA_8888_ColorType and FRAME_PIX_FMT == "rgba"
    surf.getCanvas().clear(skia.ColorSetRGB(255, 0, 0))
    assert buf[0, 0].tolist() == [255, 0, 0, 255]  # Rot liegt im ersten Kanal, wie ffmpeg "rgba" erwartet

    # So sähe es mit BGRA aus (Ursache des Fehlers): Rot landet im dritten Kanal
    bgra = np.zeros((4, 4, 4), dtype=np.uint8)
    skia.Surface(bgra, colorType=skia.kBGRA_8888_ColorType).getCanvas().clear(skia.ColorSetRGB(255, 0, 0))
    assert bgra[0, 0].tolist() == [0, 0, 255, 255]

    env = np.full(30 * 22, 0.7, dtype=np.float32)
    r = ClipRenderer(dialog_words, env, 22.0, _seats(), RenderOptions(title="Farbtest"))
    buf[:] = 0
    r.draw_frame(surf.getCanvas(), 45)
    preview = r.snapshot(45).toarray(colorType=skia.kRGBA_8888_ColorType)
    assert np.abs(buf[..., :3].astype(int) - preview[..., :3].astype(int)).mean() < 1.0
