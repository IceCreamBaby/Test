"""KI-Clip-Auswahl gegen nachgebaute API-Server (ohne echten Key/Internet)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import make_words
from podcast_animator import highlights

WORDS = make_words([
    (0, "Alter, hast du das gesehen? Das war krass!", 0.5, 4.0),
    (1, "Nein, was ist passiert? Erzähl mal.", 4.3, 7.0),
    (0, "Der Hund ist einfach auf den Tisch gesprungen und hat alles umgeworfen.", 7.3, 14.0),
    (1, "Hahaha, das ist ja verrückt! Und dann?", 14.3, 17.5),
    (0, "Dann kam der Besitzer und hat sich bei allen entschuldigt.", 17.8, 24.0),
])
CLIPS = {"clips": [{"start": 0.4, "end": 17.6, "title": "Der Hund auf dem Tisch", "reason": "Pointe", "score": 9}]}


class _Stub(BaseHTTPRequestHandler):
    requests: list = []
    mode = "ok"

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        _Stub.requests.append(("GET", self.path, dict(self.headers), None))
        if self.path.startswith("/v1beta/models"):
            self._send(200, {"models": [
                {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.1-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.1-pro", "supportedGenerationMethods": ["generateContent"]},
            ]})
        else:
            self._send(404, {"error": {"code": 404, "message": "not found", "status": "NOT_FOUND"}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        _Stub.requests.append(("POST", self.path, dict(self.headers), body))
        if _Stub.mode == "quota":
            return self._send(429, {"error": {"code": 429, "message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"}})
        if _Stub.mode == "renamed" and "gemini-flash-latest" in self.path:
            return self._send(404, {"error": {"code": 404, "message": "model not found", "status": "NOT_FOUND"}})
        self._send(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(CLIPS)}]},
                                         "finishReason": "STOP"}],
                         "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 10}})

    def log_message(self, *a):
        pass


@pytest.fixture
def gemini_stub(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", f"http://127.0.0.1:{srv.server_port}")
    _Stub.requests = []
    _Stub.mode = "ok"
    yield _Stub
    srv.shutdown()


def test_gemini_request_and_parsing(gemini_stub):
    res = highlights.find_with_gemini(WORDS, {0: "Rezo", 1: "Julien"}, 3, 10, 40, api_key="AIza-test")
    assert len(res) == 1 and res[0].source == "Gemini" and res[0].title == "Der Hund auf dem Tisch"
    assert res[0].start < 0.5 and res[0].end > 17.5  # auf Wortgrenzen eingerastet
    method, path, headers, body = gemini_stub.requests[0]
    assert path == "/v1beta/models/gemini-flash-latest:generateContent"
    assert headers.get("x-goog-api-key") == "AIza-test"
    cfg = body["generationConfig"]
    assert cfg["responseMimeType"] == "application/json"
    assert "clips" in json.dumps(cfg.get("responseSchema") or cfg.get("responseJsonSchema"))
    assert "Editor für deutschsprachige YouTube Shorts" in json.dumps(body["systemInstruction"], ensure_ascii=False)
    assert "[0.5] Rezo: Alter" in body["contents"][0]["parts"][0]["text"]


def test_gemini_renamed_model_picks_newest_flash(gemini_stub):
    gemini_stub.mode = "renamed"
    res = highlights.find_with_gemini(WORDS, {0: "Rezo", 1: "Julien"}, 3, 10, 40, api_key="AIza-test")
    assert res and res[0].source == "Gemini"
    posts = [r[1] for r in gemini_stub.requests if r[0] == "POST"]
    assert posts[-1] == "/v1beta/models/gemini-3.5-flash:generateContent"


def test_gemini_quota_falls_back_to_local(gemini_stub):
    gemini_stub.mode = "quota"
    res, note = highlights.find_clips(WORDS, None, {0: "Rezo", 1: "Julien"}, count=1, min_len=8, max_len=30,
                                      use_ai=True, provider="gemini", api_key="AIza-test")
    assert res and res[0].source == "lokal"
    assert "Gemini" in note and "Limit" in note


def test_ai_config_prefers_provider_with_key(monkeypatch):
    from podcast_animator.store import ai_config

    for v in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    cfg = ai_config({"ai_provider": "claude", "anthropic_api_key": "", "gemini_api_key": "AIza-x"})
    assert cfg["provider"] == "gemini" and cfg["available"] and cfg["api_key"] == "AIza-x"
    cfg = ai_config({"ai_provider": "claude", "anthropic_api_key": "sk-ant", "gemini_api_key": "AIza-x"})
    assert cfg["provider"] == "claude" and cfg["model"] is None
    assert not ai_config({"ai_provider": "gemini"})["available"]


class _ClaudeStub(BaseHTTPRequestHandler):
    last: dict = {}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        _ClaudeStub.last = {"body": body, "headers": dict(self.headers)}
        resp = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": json.dumps(CLIPS)}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 10}}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def test_claude_request_and_parsing(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _ClaudeStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{srv.server_port}")
    try:
        res, note = highlights.find_clips(WORDS, None, {0: "Rezo", 1: "Julien"}, count=2, min_len=8, max_len=30,
                                          use_ai=True, provider="claude", api_key="sk-ant-test")
    finally:
        srv.shutdown()
    assert res and res[0].source == "Claude" and note == "Clips von Claude ausgewählt."
    body = _ClaudeStub.last["body"]
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "high" and "format" in body["output_config"]
    assert "server-side-fallback-2026-07-01" in _ClaudeStub.last["headers"].get("anthropic-beta", "")
