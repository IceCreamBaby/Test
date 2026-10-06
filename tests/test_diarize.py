import numpy as np

from podcast_animator import diarize as D


def _unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def _synthetic(turns, rng):
    """turns: [(speaker_vector, start, end)] -> Wörter, Fensterstarts, Fenster-Embeddings."""
    words, voice_at = [], []
    for vec, s, e in turns:
        t = s
        while t + 0.3 <= e:
            words.append({"w": "wort", "s": round(t, 2), "e": round(t + 0.3, 2)})
            t += 0.4
        voice_at.append((s, e, vec))
    dur = turns[-1][2] + 1
    starts = D.speech_windows(words, dur)
    E = []
    for st in starts:
        mid = st + D.WIN / 2
        vec = next((v for s, e, v in voice_at if s <= mid < e), voice_at[-1][2])
        E.append(_unit(vec + rng.normal(0, 0.25, len(vec))))
    return words, starts, np.array(E)


def test_two_hosts_and_ad_are_separated():
    rng = np.random.default_rng(1)
    a, b, ad = _unit([1, 0.3, 0, 0]), _unit([0.2, 1, 0, 0]), _unit([0, 0, 0.3, 1])
    turns = [(ad, 0, 20)]
    t = 20.5
    for i in range(30):
        turns.append((a if i % 2 == 0 else b, t, t + 6))
        t += 6.3
    words, starts, E = _synthetic(turns, rng)
    lab, C = D._kmeans(E, 2)
    path = D.label_words(words, starts, E @ C.T)
    ad_words = [p for w, p in zip(words, path) if w["e"] < 20]
    assert np.mean(np.array(ad_words) == 2) > 0.9  # Werbung -> "Sonstige"
    # Hosts: jede Redezeile überwiegend einem Sprecher, abwechselnd
    per_turn = []
    for _vec, s, e in turns[1:]:
        labs = [p for w, p in zip(words, path) if s <= w["s"] < e]
        per_turn.append(max(set(labs), key=labs.count))
        assert labs.count(per_turn[-1]) / len(labs) > 0.85
    assert all(x != y for x, y in zip(per_turn, per_turn[1:]))


def test_choose_k_auto():
    rng = np.random.default_rng(2)
    vecs = [_unit([1, 0, 0]), _unit([0, 1, 0]), _unit([0, 0, 1])]
    E = np.array([_unit(vecs[i % 3] + rng.normal(0, 0.15, 3)) for i in range(300)])
    assert D.choose_k(E) == 3
    E1 = np.array([_unit(vecs[0] + rng.normal(0, 0.15, 3)) for _ in range(300)])
    assert D.choose_k(E1) == 1


def test_speech_windows_only_where_words():
    words = [{"w": "x", "s": 10.0, "e": 12.0}]
    st = D.speech_windows(words, 30.0)
    assert len(st) and st.min() >= 9.0 and st.max() <= 11.3


def test_profiles_ignore_other_models(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "PROFILE_FILE", tmp_path / "p.json")
    (tmp_path / "p.json").write_text('{"rezo": {"embedding": [1, 0], "count": 1, "model": "alt.onnx"}}')
    assert D.load_profiles() == {}
    D.save_profile("julien", [0.0, 1.0])
    assert D.match_profiles({0: [1.0, 0.0], 1: [0.0, 1.0]}, ["julien", "rezo"]) == {1: "julien"}


def test_name_mentions_mapping():
    from podcast_animator.pipeline import name_mentions

    def w(text, spk):
        return {"w": text, "s": 0, "e": 0.1, "spk": spk}

    words = [w("Julien,", 0), w("Julien", 0), w("Julien?", 0), w("Julien", 0), w("Rezo", 1), w("Rezo!", 1),
             w("Rezo", 1), w("Julien", 1)]
    assert name_mentions(words, [0, 1], ["rezo", "julien"]) == {0: "rezo", 1: "julien"}
    assert name_mentions(words[:2], [0, 1], ["rezo", "julien"]) == {}  # zu wenig Hinweise
