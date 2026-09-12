"""Proxy routing, env wiring, and parse-failure isolation."""

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from flightrec.events import Kind
from flightrec.proxy import LlmProxy, _split_base
from flightrec.store import create_session


def test_split_base():
    assert _split_base("https://api.openai.com/v1") == ("https://api.openai.com", "/v1")
    assert _split_base("https://h") == ("https://h", "")


def test_env_sets_all_base_urls(tmp_path: Path):
    proxy = LlmProxy(create_session(tmp_path))
    env = proxy.env({})
    assert env["ANTHROPIC_BASE_URL"].endswith("/anthropic")
    assert env["OPENAI_BASE_URL"].endswith("/openai/v1")
    assert env["OPENAI_API_BASE"] == env["OPENAI_BASE_URL"]
    assert env["FLIGHTREC_PROXY"] == proxy.base_url


def test_resolve_routes():
    proxy = LlmProxy(create_session(Path("/tmp")))
    assert proxy.resolve("/anthropic/v1/messages")[0] == "anthropic"
    assert proxy.resolve("/openai/v1/chat/completions")[2] == "/v1/chat/completions"
    host, base, path = proxy.resolve("/to/example.com/v1/x")
    assert (host, base, path) == ("example.com", "https://example.com", "/v1/x")
    assert proxy.resolve("/") is None
    assert proxy.resolve("/unknown/x") is None


def test_parse_failure_becomes_note_not_crash(tmp_path: Path):
    # Upstream returns JSON that claims to be anthropic but shape is broken;
    # the proxy must still forward it and record a NOTE, never raise.
    class Weird(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = b'{"content": "not-a-list-so-iteration-differs"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    up = HTTPServer(("127.0.0.1", 0), Weird)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    session = create_session(tmp_path)
    proxy = LlmProxy(session, upstreams={"anthropic": f"http://127.0.0.1:{up.server_port}"})
    proxy.start()
    try:
        req = urllib.request.Request(proxy.env({})["ANTHROPIC_BASE_URL"] + "/v1/messages",
                                     data=json.dumps({"model": "m", "messages": []}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
    finally:
        proxy.stop()
        up.shutdown()

    kinds = [e.kind for e in session.events()]
    assert Kind.LLM_REQUEST in kinds
    # Either it parsed into a response, or it recorded a NOTE — but it never crashed.
    assert Kind.LLM_RESPONSE in kinds or Kind.NOTE in kinds


def test_upstream_failure_returns_502(tmp_path: Path):
    proxy = LlmProxy(create_session(tmp_path),
                     upstreams={"anthropic": "http://127.0.0.1:9"})  # nothing listening
    proxy.start()
    try:
        req = urllib.request.Request(proxy.env({})["ANTHROPIC_BASE_URL"] + "/v1/messages",
                                     data=b"{}", method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 502
    finally:
        proxy.stop()
