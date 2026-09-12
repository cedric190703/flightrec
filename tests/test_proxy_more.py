"""Proxy: query strings, GET forwarding, env overrides, body cap, route tagging, lifecycle."""

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import flightrec.proxy as proxymod
from flightrec.events import Kind
from flightrec.proxy import LlmProxy
from flightrec.store import create_session


class Echo(BaseHTTPRequestHandler):
    """Upstream that answers with a description of what it received."""
    seen: list[dict] = []

    def log_message(self, *_):
        pass

    def _respond(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        Echo.seen.append({"method": self.command, "path": self.path, "body": body,
                          "host": self.headers.get("Host"),
                          "accept_encoding": self.headers.get("Accept-Encoding")})
        out = json.dumps({"model": "m", "stop_reason": "end_turn", "usage": {},
                          "content": [{"type": "text", "text": "x" * 3000}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.send_header("X-Upstream", "yes")
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = _respond


def _upstream():
    up = HTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    return up, f"http://127.0.0.1:{up.server_port}"


def test_query_string_get_and_headers_forwarded(tmp_path: Path):
    up, url = _upstream()
    Echo.seen.clear()
    session = create_session(tmp_path)
    proxy = LlmProxy(session, upstreams={"openai": url + "/v1"})
    proxy.start()
    try:
        r = urllib.request.urlopen(proxy.env({})["OPENAI_BASE_URL"] + "/models?limit=2&x=y", timeout=5)
        assert r.status == 200 and r.headers["X-Upstream"] == "yes"
        assert r.headers["Connection"] == "close"
    finally:
        proxy.stop()
        up.shutdown()
    (req,) = Echo.seen
    assert req["method"] == "GET" and req["path"] == "/v1/models?limit=2&x=y"
    assert req["host"] == f"127.0.0.1:{up.server_port}" and req["accept_encoding"] == "identity"
    ev = next(e for e in session.events() if e.kind == Kind.LLM_REQUEST)
    assert ev.payload["path"] == "/v1/models" and ev.payload["route"] == "openai"
    assert ev.payload["provider"] == "unknown" and ev.payload["status"] == 200


def test_body_cap_truncates_recording_but_not_the_client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(proxymod, "MAX_BODY_KEEP", 100)
    up, url = _upstream()
    session = create_session(tmp_path)
    proxy = LlmProxy(session, upstreams={"anthropic": url})
    proxy.start()
    try:
        req = urllib.request.Request(proxy.env({})["ANTHROPIC_BASE_URL"] + "/v1/messages",
                                     data=b'{"model":"m","messages":[]}', method="POST")
        body = urllib.request.urlopen(req, timeout=5).read()
        assert len(json.loads(body)["content"][0]["text"]) == 3000   # client got everything
    finally:
        proxy.stop()
        up.shutdown()
    kinds = [e.kind for e in session.events()]
    # The kept body is a truncated JSON document: parsed as an empty response, never a crash.
    assert kinds[0] == Kind.LLM_REQUEST and Kind.LLM_RESPONSE in kinds
    assert all(e.payload.get("route") == "anthropic" for e in session.events())


def test_env_override_becomes_upstream_unless_loopback(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gw.corp.example/anthropic/")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:5555/openai/v1")   # a stale flightrec
    proxy = LlmProxy(create_session(tmp_path))
    try:
        assert proxy.upstreams["anthropic"] == "https://gw.corp.example/anthropic"
        assert proxy.upstreams["openai"] == "https://api.openai.com"
        assert proxy.resolve("/anthropic/v1/messages") == \
            ("anthropic", "https://gw.corp.example", "/anthropic/v1/messages")
        env = proxy.env()      # defaults to os.environ, which we then override
        assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    finally:
        proxy.stop()


def test_stop_is_safe_before_start_and_twice(tmp_path: Path):
    proxy = LlmProxy(create_session(tmp_path))
    proxy.stop()
    proxy.stop()
    proxy = LlmProxy(create_session(tmp_path))
    proxy.start()
    proxy.stop()
    proxy.stop()


def test_record_never_raises_even_if_extract_does(tmp_path: Path, monkeypatch):
    session = create_session(tmp_path)
    proxy = LlmProxy(session)
    monkeypatch.setattr(proxymod, "extract", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    proxy.record("anthropic", "/v1/messages", {}, b"{}", 200, {}, b"{}", 0.0, 1.0)
    (ev,) = session.events()
    assert ev.kind == Kind.NOTE and "RuntimeError" in ev.payload["error"] and ev.payload["route"] == "anthropic"
    proxy.stop()


def test_streamed_detection_uses_content_type(tmp_path: Path):
    session = create_session(tmp_path)
    proxy = LlmProxy(session)
    sse = b'data: {"type":"message_start","message":{"model":"m","usage":{"input_tokens":2}}}\n\n'
    proxy.record("anthropic", "/v1/messages", {}, b'{"messages":[]}', 200,
                 {"content-type": "text/event-stream; charset=utf-8"}, sse, 0.0, 1.0)
    resp = next(e for e in session.events() if e.kind == Kind.LLM_RESPONSE)
    assert resp.payload["model"] == "m" and resp.payload["usage"]["input"] == 2
    proxy.stop()
