"""Exercise the proxy against a fake upstream API served in-process."""

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from flightrec.events import Kind
from flightrec.proxy import LlmProxy
from flightrec.store import create_session

FAKE_SSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"model":"m","usage":{"input_tokens":3}}}\n\n'
    'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_9","name":"bash","input":{}}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"command\\":\\"ls\\"}"}}\n\n'
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":4}}\n\n'
)


class FakeUpstream(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        FakeUpstream.seen.append({"path": self.path, "body": body,
                                  "api_key": self.headers.get("x-api-key")})
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in FAKE_SSE.split("\n\n"):
                if not piece:
                    continue
                data = (piece + "\n\n").encode()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        else:
            out = json.dumps({"model": "m", "stop_reason": "end_turn",
                              "usage": {"input_tokens": 1, "output_tokens": 1},
                              "content": [{"type": "text", "text": "hi"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)


def test_proxy_streams_and_records(tmp_path: Path):
    upstream = HTTPServer(("127.0.0.1", 0), FakeUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    up_url = f"http://127.0.0.1:{upstream.server_port}"

    session = create_session(tmp_path)
    proxy = LlmProxy(session, upstreams={"anthropic": up_url})
    proxy.start()
    try:
        env = proxy.env({})
        assert env["ANTHROPIC_BASE_URL"].endswith("/anthropic")

        def call(payload):
            req = urllib.request.Request(env["ANTHROPIC_BASE_URL"] + "/v1/messages",
                                         data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "x-api-key": "sk-test"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()

        status, body = call({"model": "m", "stream": True,
                             "messages": [{"role": "user", "content": "list"}]})
        assert status == 200 and "toolu_9" in body       # streamed through verbatim
        status, body = call({"model": "m", "messages": [
            {"role": "user", "content": "list"},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_9", "content": "a"}]}]})
        assert status == 200 and json.loads(body)["content"][0]["text"] == "hi"
    finally:
        proxy.stop()
        upstream.shutdown()

    assert FakeUpstream.seen[0]["path"] == "/v1/messages"
    assert FakeUpstream.seen[0]["api_key"] == "sk-test"   # headers forwarded

    kinds = [e.kind for e in session.events()]
    assert kinds == [Kind.LLM_REQUEST, Kind.USER_MESSAGE, Kind.LLM_RESPONSE, Kind.TOOL_CALL,
                     Kind.LLM_REQUEST, Kind.TOOL_RESULT, Kind.LLM_RESPONSE]
    call_ev = next(e for e in session.events() if e.kind == Kind.TOOL_CALL)
    assert call_ev.payload["input"] == {"command": "ls"}


def test_proxy_unknown_route(tmp_path: Path):
    proxy = LlmProxy(create_session(tmp_path))
    proxy.start()
    try:
        req = urllib.request.Request(proxy.base_url + "/nope/v1", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        proxy.stop()


def test_resolve_respects_prefix_override(tmp_path: Path):
    proxy = LlmProxy(create_session(tmp_path), upstreams={"openai": "https://gw.example/v1"})
    assert proxy.resolve("/openai/v1/chat/completions") == \
        ("openai", "https://gw.example", "/v1/chat/completions")
    assert proxy.resolve("/to/generativelanguage.googleapis.com/v1beta/x") == \
        ("generativelanguage.googleapis.com", "https://generativelanguage.googleapis.com", "/v1beta/x")
