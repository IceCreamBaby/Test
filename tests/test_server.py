"""Kompletter Ablauf über die Web-API – Spracherkennung und Sprechererkennung werden simuliert."""
import time

from fastapi.testclient import TestClient

from conftest import make_words


def test_full_flow(monkeypatch, speech_wav):
    from podcast_animator import diarize, transcribe

    words = make_words([
        (0, "Alter, hast du das gesehen? Das war krass!", 0.5, 4.0),
        (1, "Nein, was ist passiert? Erzähl mal.", 4.3, 7.0),
        (0, "Der Hund ist einfach auf den Tisch gesprungen.", 7.3, 11.0),
        (1, "Hahaha, das ist ja verrückt!", 11.3, 13.5),
    ])
    for w in words:
        w.pop("spk")
    monkeypatch.setattr(transcribe, "transcribe", lambda audio, *a, **k: {"language": "de", "words": [dict(w) for w in words]})
    segs = [{"s": 0.5, "e": 4.1, "spk": 0}, {"s": 4.2, "e": 7.1, "spk": 1}, {"s": 7.2, "e": 11.1, "spk": 0},
            {"s": 11.2, "e": 13.6, "spk": 1}]
    monkeypatch.setattr(diarize, "diarize_isolated",
                        lambda wav, n, words=None, progress=None, cancel=None: (segs, {0: [1.0, 0.0], 1: [0.0, 1.0]}))

    from podcast_animator.server import create_app

    client = TestClient(create_app())
    st = client.get("/api/state").json()
    assert {"rezo", "julien"} <= {c["id"] for c in st["characters"]}

    with open(speech_wav, "rb") as f:
        up = client.put("/api/upload", params={"filename": "folge.wav"}, content=f.read()).json()
    p = client.post("/api/projects", json={"upload_id": up["upload_id"], "filename": "folge.wav",
                                           "settings": {"mode": "single", "auto_render": True}}).json()
    pid = p["id"]
    deadline = time.time() + 180
    while time.time() < deadline:
        p = client.get(f"/api/projects/{pid}").json()
        if p["status"] == "error":
            raise AssertionError(p["error"])
        if p["clips"] and all(c["status"] == "done" for c in p["clips"]):
            break
        time.sleep(0.5)
    else:
        raise AssertionError("Rendern hat zu lange gedauert")
    assert [sp["char"] for sp in p["speakers"]] == ["rezo", "julien"]
    clip = p["clips"][0]
    video = client.get(f"/api/projects/{pid}/file", params={"path": clip["video"]})
    assert video.status_code == 200 and len(video.content) > 10000

    # Untertitel bearbeiten -> Clip ist "veraltet"
    lines = client.get(f"/api/projects/{pid}/clips/{clip['id']}/lines").json()["lines"]
    lines[0]["text"] = "Geänderter Text"
    lines[1]["spk"] = 0
    c2 = client.patch(f"/api/projects/{pid}/clips/{clip['id']}", json={"lines": lines, "title": "Neu"}).json()
    assert c2["stale"] and c2["title"] == "Neu"
    assert c2["words"][0]["w"] == "Geänderter"

    # Zeitleiste: Person hinzufügen, Sprechbereiche einzeichnen, Text für gleichzeitiges Reden
    data = client.get(f"/api/projects/{pid}/clips/{clip['id']}/lines").json()
    assert [t["char"] for t in data["tracks"]] == ["rezo", "julien"]
    assert all("orig_spk" in ln for ln in data["lines"])
    c0 = p["clips"][0]
    body = {"extra_speakers": [{"spk": 1000, "char": "gast"}],
            "regions": [{"spk": 1, "s": c0["start"] + 1.0, "e": c0["start"] + 2.5},
                        {"spk": 1000, "s": c0["start"] + 3.0, "e": c0["start"] + 4.0},
                        {"spk": 1000, "s": c0["start"] + 3.9, "e": c0["start"] + 4.5}],
            "lines": data["lines"] + [{"new": True, "spk": 1000, "s": c0["start"] + 3.0, "e": c0["start"] + 4.0,
                                       "text": "Ich bin auch da!"}]}
    c3 = client.patch(f"/api/projects/{pid}/clips/{clip['id']}", json=body).json()
    assert c3["extra_speakers"] == [{"spk": 1000, "char": "gast"}]
    assert len(c3["regions"]) == 2  # überlappende Bereiche derselben Person zusammengefasst
    assert any(w["spk"] == 1000 and w["w"] == "Ich" for w in c3["words"])
    data = client.get(f"/api/projects/{pid}/clips/{clip['id']}/lines").json()
    assert [t["char"] for t in data["tracks"]] == ["rezo", "julien", "gast"]
    bad = client.patch(f"/api/projects/{pid}/clips/{clip['id']}", json={"regions": [{"spk": 7, "s": 1, "e": 2}]})
    assert bad.status_code == 400
    wf = client.get(f"/api/projects/{pid}/waveform", params={"start": c0["start"], "end": c0["end"], "bins": 100}).json()
    assert len(wf["peaks"]) == 100 and max(wf["peaks"]) == 1.0
    # Audio muss Range können, sonst springt der Browser nicht
    span = {"start": c0["start"], "end": c0["end"]}
    full = client.get(f"/api/projects/{pid}/audio", params=span)
    assert full.headers["accept-ranges"] == "bytes"
    part = client.get(f"/api/projects/{pid}/audio", params=span, headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == full.content[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(full.content)}"
    client.post(f"/api/projects/{pid}/clips/{clip['id']}/render")
    deadline = time.time() + 180
    while time.time() < deadline:
        c4 = client.get(f"/api/projects/{pid}").json()["clips"][0]
        if c4["status"] in ("done", "error") and not c4.get("stale"):
            break
        time.sleep(0.5)
    assert c4["status"] == "done", c4.get("error")

    # Person wieder entfernen: dazugeschriebener Text fällt weg, umgestellte Zeilen bekommen den erkannten Sprecher
    data = client.get(f"/api/projects/{pid}/clips/{clip['id']}/lines").json()
    moved = next(ln for ln in data["lines"] if ln["spk"] != 1000)
    moved["spk"] = 1000
    client.patch(f"/api/projects/{pid}/clips/{clip['id']}", json={"lines": data["lines"]})
    c5 = client.patch(f"/api/projects/{pid}/clips/{clip['id']}", json={"extra_speakers": []}).json()
    assert not any(w["spk"] == 1000 or w.get("added") for w in c5["words"])
    assert any(abs(w["s"] - moved["s"]) < 0.05 and w["spk"] in (0, 1) for w in c5["words"])
    assert [r["spk"] for r in c5["regions"]] == [1]

    # Sprecher tauschen + Vorschau
    r = client.post(f"/api/projects/{pid}/speakers", json={"speakers": [{"spk": 0, "char": "julien"},
                                                                        {"spk": 1, "char": "rezo"}]}).json()
    assert r["ok"]
    img = client.get(f"/api/projects/{pid}/clips/{clip['id']}/preview.jpg", params={"t": 2})
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"

    # Pfad-Traversal wird blockiert
    assert client.get(f"/api/projects/{pid}/file", params={"path": "../../settings.json"}).status_code == 404
    assert client.delete(f"/api/projects/{pid}").json()["ok"]
