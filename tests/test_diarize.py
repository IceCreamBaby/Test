import numpy as np

from podcast_animator.diarize import match_profiles, merge_clusters


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return (v / np.linalg.norm(v)).tolist()


def test_ad_voice_does_not_steal_a_host_slot():
    # Cluster 0/1 = Host A (zwei Teil-Cluster), 2 = Host B, 3 = Werbung (fremd, kurz)
    segs = [{"s": 0, "e": 30, "spk": 3}]
    t = 30.0
    for i in range(60):
        spk = [0, 2, 1, 2][i % 4]
        segs.append({"s": t, "e": t + 8, "spk": spk})
        t += 8.2
    embs = {0: _unit([1, 0.1, 0]), 1: _unit([0.9, 0.25, 0.05]), 2: _unit([0.05, 1, 0.1]), 3: _unit([0, 0.1, 1])}
    out = merge_clusters(segs, embs, 2)
    labels = {s["spk"] for s in out}
    assert labels == {0, 1, 2}  # zwei Hosts + "Sonstige"
    ad = [s for s in out if s["s"] == 0][0]
    assert ad["spk"] == 2 and ad.get("other")
    talk = {k: sum(s["e"] - s["s"] for s in out if s["spk"] == k) for k in labels}
    assert abs(talk[0] - talk[1]) < 0.2 * talk[0]  # Hosts etwa gleich viel


def test_auto_mode_finds_distinct_voices():
    segs = [{"s": i * 10, "e": i * 10 + 9, "spk": i % 3} for i in range(30)]
    embs = {0: _unit([1, 0, 0]), 1: _unit([0, 1, 0]), 2: _unit([0, 0, 1])}
    out = merge_clusters(segs, embs, 0)
    assert {s["spk"] for s in out} == {0, 1, 2}
    assert not any(s.get("other") for s in out)


def test_match_profiles_greedy():
    embs = {0: _unit([1, 0]), 1: _unit([0, 1])}
    import podcast_animator.diarize as d

    orig = d.load_profiles
    d.load_profiles = lambda: {"rezo": {"embedding": _unit([0.1, 1])}, "julien": {"embedding": _unit([1, 0.2])}}
    try:
        assert match_profiles(embs, ["rezo", "julien"]) == {1: "rezo", 0: "julien"}
    finally:
        d.load_profiles = orig
