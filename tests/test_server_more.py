"""Viewer API: listing order, diff parameters, blob content types, 404s."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from flightrec.events import Event, Kind, Snapshot, Source
from flightrec.server import Api, make_server, session_detail, session_summary, unified_diff
from flightrec.store import create_session


def _serve(home):
    srv = make_server(home)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def _get(url):
    return urllib.request.urlopen(url, timeout=5)


def test_sessions_listed_newest_first(tmp_path: Path):
    a = create_session(tmp_path)
    b = create_session(tmp_path)
    a.start(["x"], "/"); b.start(["y"], "/")
    status, ctype, body = Api(tmp_path).handle("/api/sessions", {})
    ids = [s["id"] for s in json.loads(body)]
    assert status == 200 and ids == sorted([a.id, b.id], reverse=True)
    assert json.loads(body)[0]["events"] == 1


def test_session_detail_contains_files_and_serialisable_events(tmp_path: Path):
    s = create_session(tmp_path)
    sha = s.blobs.put(b"x")
    s.append(Event(Kind.FS_CHANGE, Source.FS, {"path": "a", "op": "create", "size": 1},
                   snapshots=[Snapshot("a", None, sha)]))
    d = json.loads(json.dumps(session_detail(s)))
    assert d["files"] == {"a": sha}
    assert d["events"][0]["snapshots"][0]["after"] == sha
    assert d["steps"][0]["kind"] == "fs"
    assert session_summary(s)["events"] == 1


def test_diff_endpoint_defaults_and_counts(tmp_path: Path):
    s = create_session(tmp_path)
    before = s.blobs.put(b"a\nb\nc\n")
    after = s.blobs.put(b"a\nB\nc\nd\n")
    api = Api(tmp_path)
    _, _, body = api.handle(f"/api/sessions/{s.id}/diff", {"before": [before], "after": [after]})
    d = json.loads(body)
    assert d["added"] == 2 and d["removed"] == 1 and "--- a/file" in d["diff"]
    _, _, body = api.handle(f"/api/sessions/{s.id}/diff", {"after": [after], "path": ["n.txt"]})
    d = json.loads(body)
    assert d["removed"] == 0 and d["added"] == 4 and "+++ b/n.txt" in d["diff"]
    _, _, body = api.handle(f"/api/sessions/{s.id}/diff", {})
    assert json.loads(body) == {"binary": False, "diff": "", "added": 0, "removed": 0}


def test_unified_diff_identical_content_is_empty(tmp_path: Path):
    s = create_session(tmp_path)
    sha = s.blobs.put(b"same\n")
    assert unified_diff(s, sha, sha, "f")["diff"] == ""


def test_blob_content_types_and_unknown_routes(tmp_path: Path):
    s = create_session(tmp_path)
    text = s.blobs.put("héllo\n".encode())
    binary = s.blobs.put(b"\x00\xff")
    srv, base = _serve(tmp_path)
    try:
        r = _get(f"{base}/api/sessions/{s.id}/blob/{text}")
        assert r.headers["Content-Type"] == "text/plain; charset=utf-8" and r.read().decode() == "héllo\n"
        r = _get(f"{base}/api/sessions/{s.id}/blob/{binary}")
        assert r.headers["Content-Type"] == "application/octet-stream"
        for bad in [f"/api/sessions/{s.id}/blob", f"/api/sessions/{s.id}/blob/{text}/x",
                    f"/api/sessions/{s.id}/unknown", "/api/", "/api/nothing", "/nope.html"]:
            try:
                _get(base + bad)
                assert False, bad
            except urllib.error.HTTPError as e:
                assert e.code == 404, bad
    finally:
        srv.shutdown()


def test_query_string_on_page_does_not_break_static(tmp_path: Path):
    create_session(tmp_path)
    srv, base = _serve(tmp_path)
    try:
        assert b"<title>flightrec" in _get(base + "/?x=1").read()
        assert b"<title>flightrec" in _get(base + "/index.html").read()
    finally:
        srv.shutdown()
