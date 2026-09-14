"""LLM observer: a local reverse proxy in front of model APIs.

The harness is pointed at us through the SDK base-URL env vars that
virtually every client honours (``ANTHROPIC_BASE_URL``, ``OPENAI_BASE_URL``,
...). We forward each request byte-for-byte to the real upstream, stream
the response straight back, and only *after* the response is complete do we
parse both bodies into events. A parser failure can never break the agent.

Routes:
    /anthropic/<path>     -> $ANTHROPIC_BASE_URL or https://api.anthropic.com
    /openai/<path>        -> $OPENAI_BASE_URL    or https://api.openai.com
    /to/<host>/<path>     -> https://<host>/<path>   (any other provider)

No TLS interception: the harness talks plain HTTP to 127.0.0.1 and we talk
HTTPS upstream, so no certificates are needed.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .events import Event, Kind, Source
from .redact import Redactor
from .store import Session
from .wire import Dedup, detect_provider, extract

DEFAULT_UPSTREAMS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
}
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
              "accept-encoding", "content-encoding"}
MAX_BODY_KEEP = 8 * 1024 * 1024


def _split_base(url: str) -> tuple[str, str]:
    """'https://host:443/v1' -> ('https://host:443', '/v1')"""
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}", u.path.rstrip("/")


class LlmProxy:
    def __init__(self, session: Session, upstreams: dict[str, str] | None = None,
                 host: str = "127.0.0.1", port: int = 0,
                 redactor: Redactor | None = None):
        self.session = session
        self.redactor = redactor if redactor is not None else Redactor.from_env()
        self.upstreams = dict(DEFAULT_UPSTREAMS)
        # Honour a pre-existing override (e.g. a corporate gateway) as the upstream.
        for name, var in (("anthropic", "ANTHROPIC_BASE_URL"), ("openai", "OPENAI_BASE_URL")):
            if os.environ.get(var) and "127.0.0.1" not in os.environ[var]:
                self.upstreams[name] = os.environ[var].rstrip("/")
        if upstreams:
            self.upstreams.update(upstreams)
        self.dedup = Dedup()
        self._lock = threading.Lock()
        proxy = self
        # Explicit IPv4 so a client resolving "localhost" to ::1 never mismatches.

        class Handler(_Handler):
            proxy_ref = proxy

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._closed = False

    @property
    def base_url(self) -> str:
        h, p = self._server.server_address[:2]
        return f"http://{h}:{p}"

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("cannot restart a stopped LlmProxy")
        if self._thread.is_alive():
            return
        self._thread.start()

    def stop(self) -> None:
        # shutdown() blocks until serve_forever() acknowledges, so it must
        # only be called when the loop is actually running.
        if self._closed:
            return
        self._closed = True
        if self._thread.is_alive():
            self._server.shutdown()
            self._thread.join(timeout=2)
        self._server.server_close()

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(base if base is not None else os.environ)
        env["ANTHROPIC_BASE_URL"] = f"{self.base_url}/anthropic"
        env["OPENAI_BASE_URL"] = f"{self.base_url}/openai/v1"
        env["OPENAI_API_BASE"] = env["OPENAI_BASE_URL"]      # older SDKs / aider
        env["FLIGHTREC_PROXY"] = self.base_url
        return env

    def resolve(self, path: str) -> tuple[str, str, str] | None:
        """Map an incoming path to (route, upstream_base, upstream_path)."""
        parts = path.split("/", 2)
        if len(parts) < 2:
            return None
        route = parts[1]
        rest = "/" + parts[2] if len(parts) > 2 else "/"
        if route in self.upstreams:
            base, prefix = _split_base(self.upstreams[route])
            # Avoid doubling a prefix already present in both the override and the SDK path.
            if prefix and rest.startswith(prefix + "/"):
                prefix = ""
            return route, base, prefix + rest
        if route == "to" and len(parts) > 2:
            host, _, sub = parts[2].partition("/")
            return host, f"https://{host}", "/" + sub
        return None

    def record(self, route: str, path: str, headers: dict[str, str], req_body: bytes,
               status: int, resp_headers: dict[str, str], resp_body: bytes,
               ts_req: float, ts_resp: float) -> None:
        provider = detect_provider(path, headers)
        ctype = resp_headers.get("content-type", "")
        streamed = "text/event-stream" in ctype
        # Scrub credentials before the parsers run, so no event can ever carry
        # a key even if a body embeds one in an unexpected place.
        req_text = self.redactor.text(req_body.decode("utf-8", "replace"))
        resp_text = self.redactor.text(resp_body.decode("utf-8", "replace"))
        try:
            events = extract(provider, path, req_text, resp_text, status, streamed,
                             self.dedup, ts_req, ts_resp)
        except Exception as exc:  # noqa: BLE001 - never break the harness
            events = [Event(Kind.NOTE, Source.PROXY,
                            {"error": f"parse failure: {exc!r}", "path": path, "route": route})]
        with self._lock:
            for ev in events:
                ev.payload.setdefault("route", route)
                self.session.append(ev)


def _error_body(message: str) -> bytes:
    return json.dumps({"error": message}).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    proxy_ref: LlmProxy
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):  # silence default stderr logging
        pass

    def _forward(self) -> None:
        ts_req = time.time()
        resolved = self.proxy_ref.resolve(self.path.split("?")[0])
        if resolved is None:
            self._reply(404, _error_body("flightrec: unknown route"))
            return
        route, base, up_path = resolved
        if "?" in self.path:
            up_path += "?" + self.path.split("?", 1)[1]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self._reply(400, _error_body("flightrec: bad Content-Length"))
            return
        body = self.rfile.read(length) if length else b""

        u = urlsplit(base)
        conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        kwargs = {"timeout": 600}
        if u.scheme == "https":
            kwargs["context"] = ssl.create_default_context()
        conn = conn_cls(u.netloc, **kwargs)

        fwd = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        fwd["Host"] = u.netloc
        fwd["Accept-Encoding"] = "identity"
        if body:
            fwd["Content-Length"] = str(len(body))
        try:
            conn.request(self.command, up_path, body=body or None, headers=fwd)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            self._reply(502, _error_body(f"flightrec upstream failure: {exc}"))
            return

        self.send_response(resp.status, resp.reason)
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        for k, v in resp.getheaders():
            if k.lower() in HOP_BY_HOP:
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()

        chunks: list[bytes] = []
        kept = 0
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                if kept < MAX_BODY_KEEP:
                    chunks.append(chunk)
                    kept += len(chunk)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            conn.close()
        self.close_connection = True
        self.proxy_ref.record(route, up_path.split("?")[0], dict(self.headers.items()), body,
                              resp.status, resp_headers, b"".join(chunks), ts_req, time.time())

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _forward
