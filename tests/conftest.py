import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

# Eigener Datenordner für die Tests (vor dem Import des Pakets setzen)
_TMP = tempfile.mkdtemp(prefix="pa-test-")
os.environ["PODCAST_ANIMATOR_DATA"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def make_words(turns):
    """turns: [(spk, "text ...", start, end)] -> Wörter mit gleichmäßig verteilten Zeiten."""
    words = []
    for spk, text, s, e in turns:
        toks = text.split()
        step = (e - s) / len(toks)
        for i, t in enumerate(toks):
            words.append({"w": t, "s": round(s + i * step, 3), "e": round(s + (i + 0.85) * step, 3), "p": 1.0,
                          "spk": spk})
    return words


@pytest.fixture
def dialog_words():
    return make_words([
        (0, "Alter, hast du das gesehen? Das war krass!", 0.0, 3.0),
        (1, "Nein, was ist passiert? Erzähl mal.", 3.3, 5.5),
        (0, "Also ich war gestern im Supermarkt und dann kommt da so ein Typ rein.", 5.8, 10.0),
        (1, "Hahaha nein, ernsthaft?", 10.2, 11.5),
        (0, "Doch, wirklich! Und der hatte einen Hund dabei.", 11.7, 14.5),
        (1, "Okay, und dann?", 14.8, 15.8),
        (0, "Dann ist der Hund einfach auf den Tisch gesprungen. Unglaublich.", 16.0, 20.0),
        (1, "Das ist ja verrückt. Haha.", 20.3, 22.0),
    ])


@pytest.fixture
def speech_wav(tmp_path):
    """Synthetisches 'Sprach'-Signal: Töne mit Pausen (16 kHz Mono WAV)."""
    from podcast_animator import media

    sr = 16000
    t = np.arange(int(sr * 24)) / sr
    sig = 0.3 * np.sin(2 * np.pi * 180 * t) * (np.sin(2 * np.pi * 3 * t) > -0.3)
    sig[(t > 3.0) & (t < 3.3)] = 0
    path = tmp_path / "speech.wav"
    media.write_wav(path, sig.astype(np.float32), sr)
    return path
