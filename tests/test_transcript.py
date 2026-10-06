from podcast_animator import transcript as T


def test_assign_speakers_by_overlap():
    words = [{"w": "a", "s": 0.0, "e": 0.4}, {"w": "b", "s": 1.0, "e": 1.4}, {"w": "c", "s": 5.0, "e": 5.2}]
    segs = [{"s": 0.0, "e": 0.9, "spk": 1}, {"s": 0.9, "e": 3.0, "spk": 0}]
    T.assign_speakers(words, segs)
    assert [w["spk"] for w in words] == [1, 0, 0]  # letztes Wort: nächstgelegene Region


def test_subtitle_chunks_split_on_speaker_and_length(dialog_words):
    chunks = T.subtitle_chunks(dialog_words, max_words=3)
    assert all(len(c.words) <= 3 for c in chunks)
    assert all(len({w["spk"] for w in c.words}) == 1 for c in chunks)
    assert sum(len(c.words) for c in chunks) == len(dialog_words)


def test_sentences_and_utterances(dialog_words):
    sents = T.sentences(dialog_words)
    assert sents[0].text == "Alter, hast du das gesehen?"
    utts = T.utterances(dialog_words)
    assert [u.spk for u in utts] == [0, 1, 0, 1, 0, 1, 0, 1]


def test_retime_text_keeps_span():
    old = [{"w": "hallo", "s": 1.0, "e": 1.4, "spk": 0}, {"w": "welt", "s": 1.5, "e": 2.0, "spk": 0}]
    new = T.retime_text(old, "Hallo liebe Welt!", spk=1)
    assert [w["w"] for w in new] == ["Hallo", "liebe", "Welt!"]
    assert new[0]["s"] == 1.0 and new[-1]["e"] <= 2.0
    assert all(w["spk"] == 1 for w in new)


def test_srt_format(dialog_words):
    srt = T.to_srt(dialog_words, {0: "Rezo", 1: "Julien"})
    assert srt.startswith("1\n00:00:00,000 --> ")
    assert "Rezo: " in srt and "Julien: " in srt


def test_laugh_detection():
    assert T.is_laugh("Hahaha") and T.is_laugh("(lacht)") and not T.is_laugh("Hallo")


def test_cli_defaults_to_serve(monkeypatch):
    from podcast_animator import cli

    seen = {}
    monkeypatch.setattr(cli, "cmd_serve", lambda args: seen.update(port=args.port, nb=args.no_browser))
    cli.main(["--port", "7861", "--no-browser"])
    assert seen == {"port": 7861, "nb": True}
    cli.main([])
    assert seen["port"] == 7860


def test_transcribe_resume_offsets(monkeypatch):
    """Fortsetzen: bereits fertige Wörter bleiben, neue Wörter werden um den Startpunkt verschoben."""
    import types

    import numpy as np

    from podcast_animator import transcribe as tr

    seen = {}

    class FakeModel:
        def transcribe(self, audio, **kw):
            seen["len"] = len(audio)
            w = types.SimpleNamespace(word=" neu", start=1.0, end=1.5, probability=0.9)
            seg = types.SimpleNamespace(words=[w], end=2.0)
            return iter([seg]), types.SimpleNamespace(language="de")

    monkeypatch.setattr(tr, "_load_model", lambda *a: FakeModel())
    audio = np.zeros(16000 * 10, dtype=np.float32)
    old = [{"w": "alt", "s": 0.5, "e": 1.0, "p": 1.0}, {"w": "weg", "s": 4.2, "e": 4.8, "p": 1.0}]
    res = tr.transcribe(audio, "small", "de", device="cpu", resume={"words": old, "done_until": 4.0})
    assert seen["len"] == 16000 * 6
    assert [w["w"] for w in res["words"]] == ["alt", "neu"]
    assert res["words"][1]["s"] == 5.0
