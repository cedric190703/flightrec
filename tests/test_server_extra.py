"""Server robustness: path traversal, binary diffs, missing blobs, static files."""

import threading
import urllib.error
import urllib.request
from pathlib import Path

from flightrec.events import Event, Kind, Snapshot, Source
from flightrec.server import make_server, unified_diff
from flightrec.store import create_session


def _serve(home):
    srv = make_server(home)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def test_path_traversal_is_blocked(tmp_path: Path):
    create_session(tmp_path)  # ensures sessions dir exists
    srv, base = _serve(tmp_path)
    try:
        for evil in ["/../../../../etc/passwd", "/..%2f..%2fetc%2fpasswd"]:
            try:
                urllib.request.urlopen(base + evil, timeout=5)
                assert False, f"expected 404 for {evil}"
            except urllib.error.HTTPError as e:
                assert e.code == 404
    finally:
        srv.shutdown()


def test_binary_diff_reported_not_decoded(tmp_path: Path):
    s = create_session(tmp_path)
    before = s.blobs.put(b"\x00\x01\x02binary")
    after = s.blobs.put(b"\x00\x01\x02\x03longer")
    d = unified_diff(s, before, after, "logo.png")
    assert d["binary"] is True
    assert d["before_size"] == 9 and d["after_size"] == 10


def test_diff_with_missing_blob_is_treated_as_empty(tmp_path: Path):
    s = create_session(tmp_path)
    after = s.blobs.put(b"new content\n")
    d = unified_diff(s, "deadbeef" * 8, after, "f.txt")   # 'before' blob absent
    assert d["binary"] is False and d["added"] == 1 and d["removed"] == 0


def test_blob_404_for_unknown_hash(tmp_path: Path):
    s = create_session(tmp_path)
    srv, base = _serve(tmp_path)
    try:
        try:
            urllib.request.urlopen(f"{base}/api/sessions/{s.id}/blob/doesnotexist", timeout=5)
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()


def test_static_viewer_served_with_html_type(tmp_path: Path):
    create_session(tmp_path)
    srv, base = _serve(tmp_path)
    try:
        r = urllib.request.urlopen(base + "/", timeout=5)
        assert r.headers["Content-Type"].startswith("text/html")
        assert r.headers["Cache-Control"] == "no-store"
    finally:
        srv.shutdown()
