"""End-to-end: a key placed in the body/prompt never lands in the session.

Mirrors the acceptance test in next_path.md 1.1 — drive a real request
through the proxy against a fake upstream, then grep the whole session
directory for the secret.
"""

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from flightrec.proxy import LlmProxy
from flightrec.redact import REDACTED, Redactor
from flightrec.store import create_session

SECRET = "sk-ant-api03-SECRET0000abcdEFGH1234567890abcd"


class _Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        # Echo the secret back in the response body too, to prove response
        # bodies are scrubbed as well as requests.
        out = json.dumps({
            "model": "m", "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "text", "text": f"leaked {SECRET} back"}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def _serve():
    up = HTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    return up


def _drive(proxy: LlmProxy):
    body = json.dumps({
        "model": "m",
        "messages": [{"role": "user", "content": f"here is my key {SECRET} please use it"}],
    }).encode()
    req = urllib.request.Request(
        f"{proxy.base_url}/anthropic/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": SECRET})
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def test_secret_is_absent_from_the_whole_session(tmp_path: Path):
    up = _serve()
    session = create_session(tmp_path / "home")
    proxy = LlmProxy(session, upstreams={"anthropic": f"http://127.0.0.1:{up.server_port}"})
    proxy.start()
    try:
        _drive(proxy)
    finally:
        proxy.stop()
        up.shutdown()

    # The user message and assistant text were captured...
    kinds = {e.kind for e in session.events()}
    assert "user_message" in kinds
    assert "llm_response" in kinds

    # ...but the secret is nowhere on disk, and «redacted» is.
    blob = ""
    for f in session.dir.rglob("*"):
        if f.is_file():
            blob += f.read_text(encoding="utf-8", errors="replace")
    assert SECRET not in blob
    assert REDACTED in blob


def test_disabling_redaction_lets_the_secret_through(tmp_path: Path):
    up = _serve()
    session = create_session(tmp_path / "home")
    proxy = LlmProxy(session, upstreams={"anthropic": f"http://127.0.0.1:{up.server_port}"},
                     redactor=Redactor(enabled=False))
    proxy.start()
    try:
        _drive(proxy)
    finally:
        proxy.stop()
        up.shutdown()

    events_text = (session.dir / "events.jsonl").read_text(encoding="utf-8")
    assert SECRET in events_text  # the escape hatch really does disable scrubbing
