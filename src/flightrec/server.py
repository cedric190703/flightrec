"""Local web viewer: ``flightrec view``.

Serves the single-page UI from ``viewer/index.html`` and a tiny JSON API
over the session store. Everything is computed on request from the raw
event log; nothing is cached or written.
"""

from __future__ import annotations

import difflib
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .store import DEFAULT_ROOT, list_sessions, open_session
from .timeline import build_steps, files_at

VIEWER_DIR = Path(__file__).parent / "viewer"


def _is_text(data: bytes) -> bool:
    return b"\x00" not in data[:8000]


def session_summary(s) -> dict:
    meta = s.read_meta()
    return {"id": s.id, "events": len(s), **meta}


def session_detail(s) -> dict:
    events = list(s.events())
    steps = build_steps(events)
    return {
        "meta": session_summary(s),
        "steps": [st.to_dict() for st in steps],
        "events": [json.loads(e.to_json()) for e in events],
        "files": files_at(events),
    }


def unified_diff(s, before: str | None, after: str | None, path: str) -> dict:
    def load(sha):
        if not sha or not s.blobs.has(sha):
            return b""
        return s.blobs.get(sha)
    a, b = load(before), load(after)
    if not (_is_text(a) and _is_text(b)):
        return {"binary": True, "before_size": len(a), "after_size": len(b)}
    al = a.decode("utf-8", "replace").splitlines(keepends=True)
    bl = b.decode("utf-8", "replace").splitlines(keepends=True)
    diff = "".join(difflib.unified_diff(al, bl, fromfile=f"a/{path}", tofile=f"b/{path}", n=3))
    return {"binary": False, "diff": diff,
            "added": sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")),
            "removed": sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))}


class Api:
    def __init__(self, root: Path):
        self.root = root

    def handle(self, path: str, query: dict[str, list[str]]) -> tuple[int, str, bytes]:
        parts = [p for p in path.split("/") if p]
        if parts == ["api", "sessions"]:
            body = [session_summary(s) for s in reversed(list_sessions(self.root))]
            return 200, "application/json", json.dumps(body).encode()
        if len(parts) >= 3 and parts[:2] == ["api", "sessions"]:
            try:
                s = open_session(parts[2], self.root)
            except FileNotFoundError:
                return 404, "application/json", b'{"error":"no such session"}'
            if len(parts) == 3:
                return 200, "application/json", json.dumps(session_detail(s)).encode()
            if parts[3] == "blob" and len(parts) == 5:
                if not s.blobs.has(parts[4]):
                    return 404, "text/plain", b"no such blob"
                data = s.blobs.get(parts[4])
                return 200, ("text/plain; charset=utf-8" if _is_text(data)
                             else "application/octet-stream"), data
            if parts[3] == "diff":
                d = unified_diff(s, query.get("before", [None])[0], query.get("after", [None])[0],
                                 query.get("path", ["file"])[0])
                return 200, "application/json", json.dumps(d).encode()
        return 404, "application/json", b'{"error":"not found"}'


def make_server(root: Path = DEFAULT_ROOT, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    api = Api(root)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            u = urlsplit(self.path)
            if u.path.startswith("/api/"):
                try:
                    status, ctype, body = api.handle(u.path, parse_qs(u.query))
                except Exception as exc:  # noqa: BLE001 - a damaged session must answer, not hang
                    status, ctype = 500, "application/json"
                    body = json.dumps({"error": f"internal error: {exc!r}"}).encode()
            else:
                rel = "index.html" if u.path in ("", "/") else u.path.lstrip("/")
                f = (VIEWER_DIR / rel).resolve()
                if VIEWER_DIR.resolve() in f.parents and f.is_file():
                    status, body = 200, f.read_bytes()
                    ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                else:
                    status, ctype, body = 404, "text/plain", b"not found"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv
