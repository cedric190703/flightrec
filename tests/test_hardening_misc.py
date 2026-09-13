"""Cross-component hardening: exec log records with bad fields, proxy request
validation, server error isolation, fork path escapes, tolerant timeline
and CLI rendering, and OpenAI content-part flattening."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from flightrec.cli import main
from flightrec.events import Event, Kind, Snapshot, Source
from flightrec.fork import _inside, materialize
from flightrec.proxy import LlmProxy, _error_body
from flightrec.server import make_server, unified_diff
from flightrec.shim import ExecCollector, _timestamp
from flightrec.store import create_session
from flightrec.timeline import build_steps
from flightrec.wire import Dedup, extract


# -- exec collector ------------------------------------------------------

def test_timestamp_coercion():
    assert _timestamp(1700000000) == 1700000000.0
    assert _timestamp(12.5) == 12.5
    now_ish = _timestamp("yesterday")
    assert isinstance(now_ish, float) and now_ish > 1_600_000_000
    for bad in [None, True, -1, 0, [], {}]:
        assert _timestamp(bad) > 1_600_000_000, bad


def test_collector_ignores_records_without_usable_id(tmp_path: Path):
    session = create_session(tmp_path / "home")
    c = ExecCollector(session, tmp_path / "exec.jsonl")
    c._handle({"phase": "start", "argv": ["x"]})
    c._handle({"id": "", "phase": "start"})
    c._handle({"id": 7, "phase": "end", "exit_code": 0})
    assert list(session.events()) == []


def test_collector_tolerates_wrong_typed_fields(tmp_path: Path):
    session = create_session(tmp_path / "home")
    c = ExecCollector(session, tmp_path / "exec.jsonl")
    c._handle({"id": "a", "phase": "start", "argv": "not-a-list", "real": "/bin/x", "ts": "soon"})
    c._handle({"id": "a", "phase": "end", "exit_code": "zero", "ts": None})
    start, end = session.events()
    assert start.payload["argv"] == [] and start.payload["command"] == "x"
    assert end.payload["exit_code"] is None and end.payload["duration"] >= 0
    assert end.links == [start.id]


def test_collector_end_before_start_ts_clamps_duration(tmp_path: Path):
    session = create_session(tmp_path / "home")
    c = ExecCollector(session, tmp_path / "exec.jsonl")
    c._handle({"id": "a", "phase": "start", "real": "/bin/x", "ts": 100.0})
    c._handle({"id": "a", "phase": "end", "exit_code": 0, "ts": 99.0})   # clock went backwards
    _, end = session.events()
    assert end.payload["duration"] == 0.0


def test_collector_thread_survives_bad_record_and_notes_it(tmp_path: Path, monkeypatch):
    session = create_session(tmp_path / "home")
    log = tmp_path / "exec.jsonl"
    c = ExecCollector(session, log)
    real_handle = c._handle
    seen = []

    def flaky(rec):
        seen.append(rec)
        if rec.get("id") == "boom":
            raise RuntimeError("synthetic")
        real_handle(rec)

    monkeypatch.setattr(c, "_handle", flaky)
    c.start()
    try:
        log.write_text(json.dumps({"id": "boom", "phase": "start"}) + "\n")
        import time
        deadline = time.time() + 2
        while len(seen) < 1 and time.time() < deadline:
            time.sleep(0.02)
        with log.open("a") as f:
            f.write(json.dumps({"id": "ok", "phase": "start", "real": "/bin/echo", "argv": ["hi"]}) + "\n")
        while len(seen) < 2 and time.time() < deadline:
            time.sleep(0.02)
    finally:
        c.stop()
    kinds = [e.kind for e in session.events()]
    assert Kind.NOTE in kinds and Kind.EXEC in kinds
    exec_ev = next(e for e in session.events() if e.kind == Kind.EXEC)
    assert exec_ev.payload["command"] == "echo hi"


# -- proxy ---------------------------------------------------------------

def test_error_body_is_valid_json_even_with_quotes():
    body = _error_body('upstream said "no" \\ bye')
    assert json.loads(body)["error"] == 'upstream said "no" \\ bye'


def _raw_http(port: int, request: bytes) -> bytes:
    import socket
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request)
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks)


@pytest.mark.parametrize("length", ["abc", "-5", "1e3"])
def test_proxy_rejects_bad_content_length_with_400(tmp_path: Path, length):
    proxy = LlmProxy(create_session(tmp_path))
    proxy.start()
    try:
        port = proxy._server.server_address[1]
        req = (f"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\n"
               f"Content-Length: {length}\r\n\r\n").encode()
        resp = _raw_http(port, req)
        head, _, body = resp.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 400"), head
        assert json.loads(body)["error"] == "flightrec: bad Content-Length"
    finally:
        proxy.stop()
    assert list(proxy.session.events()) == []   # nothing to record for a rejected request


# -- server --------------------------------------------------------------

def _serve(home):
    srv = make_server(home)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def _get(url):
    try:
        r = urllib.request.urlopen(url, timeout=5)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_diff_endpoint_cannot_read_outside_blob_store(tmp_path: Path):
    s = create_session(tmp_path)
    s.write_meta(command=["x"], cwd="/", token="SEKRET-VALUE")
    (tmp_path / "outside.txt").write_text("OUTSIDE-VALUE")
    d = unified_diff(s, None, "../meta.json", "f")
    assert d["diff"] == "" and "SEKRET" not in json.dumps(d)

    srv, base = _serve(tmp_path)
    try:
        for after in ["../meta.json", "../../../outside.txt", "..%2Fmeta.json", "."]:
            status, body = _get(f"{base}/api/sessions/{s.id}/diff?after={after}&path=f")
            assert status == 200
            assert b"SEKRET" not in body and b"OUTSIDE" not in body
        status, body = _get(f"{base}/api/sessions/{s.id}/blob/..%2Fmeta.json")
        assert status == 404 and b"SEKRET" not in body
    finally:
        srv.shutdown()


def test_session_id_traversal_in_api_is_404(tmp_path: Path):
    create_session(tmp_path)
    srv, base = _serve(tmp_path)
    try:
        for sid in ["..", "..%2F..", "%2E%2E", "a%2Fb"]:
            status, body = _get(f"{base}/api/sessions/{sid}")
            assert status == 404, sid
            assert json.loads(body)["error"] == "no such session"
    finally:
        srv.shutdown()
    assert not (tmp_path / "blobs").exists()


def test_api_exception_yields_500_json_not_dropped_connection(tmp_path: Path, monkeypatch):
    create_session(tmp_path)
    import flightrec.server as server

    def boom(*a, **k):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(server, "list_sessions", boom)
    srv, base = _serve(tmp_path)
    try:
        status, body = _get(f"{base}/api/sessions")
        assert status == 500 and "synthetic" in json.loads(body)["error"]
        status, _ = _get(f"{base}/")          # the static page still works
        assert status == 200
    finally:
        srv.shutdown()


def test_session_detail_with_corrupt_meta_and_bad_event_line(tmp_path: Path):
    s = create_session(tmp_path)
    s.start(["x"], "/")
    with (s.dir / "events.jsonl").open("a") as f:
        f.write("not json at all\n")
    s.end(0)
    s.meta_path.write_text("{{{")
    srv, base = _serve(tmp_path)
    try:
        status, body = _get(f"{base}/api/sessions/{s.id}")
        assert status == 200
        detail = json.loads(body)
        assert [st["kind"] for st in detail["steps"]] == ["session", "session"]
        assert detail["meta"]["id"] == s.id and detail["meta"]["events"] == 3
    finally:
        srv.shutdown()


# -- fork ----------------------------------------------------------------

def test_inside_rejects_escapes(tmp_path: Path):
    dest = tmp_path.resolve()
    assert _inside(dest, "a/b.txt") == dest / "a" / "b.txt"
    assert _inside(dest, "a/../b.txt") == dest / "b.txt"
    for bad in ["../x", "a/../../x", "/etc/passwd", "", "."]:
        assert _inside(dest, bad) is None, bad


def test_materialize_never_writes_outside_dest(tmp_path: Path):
    s = create_session(tmp_path / "home")
    sha = s.blobs.put(b"evil")
    for path in ["../escaped.txt", "/tmp/abs.txt", "ok/../../escaped2.txt", "kept.txt"]:
        s.append(Event(Kind.FS_CHANGE, Source.FS, {"path": path, "op": "create", "size": 4},
                       snapshots=[Snapshot(path, None, sha)]))
    dest = tmp_path / "fork"
    written = materialize(s, 10, dest)
    assert written == ["kept.txt"]
    assert not (tmp_path / "escaped.txt").exists() and not (tmp_path / "escaped2.txt").exists()
    assert (dest / "kept.txt").read_bytes() == b"evil"


# -- timeline / cli ------------------------------------------------------

def test_build_steps_tolerates_fs_event_without_path_or_op():
    evs = [Event(Kind.FS_CHANGE, Source.FS, {}, ts=1.0, seq=0),
           Event(Kind.EXEC, Source.SHIM, {}, ts=2.0, seq=1)]
    steps = build_steps(evs)
    assert [st.kind for st in steps] == ["fs", "exec"]
    assert steps[0].fs[0]["path"] == "?" and steps[0].fs[0]["op"] == "modify"
    assert steps[1].title == "$ "


def test_show_renders_events_with_missing_payload_fields(tmp_path: Path, capsys):
    s = create_session(tmp_path)
    for kind in [Kind.FS_CHANGE, Kind.USER_MESSAGE, Kind.TOOL_CALL, Kind.SESSION_START,
                 Kind.SESSION_END, Kind.EXEC, Kind.LLM_RESPONSE, Kind.TOOL_RESULT]:
        s.append(Event(kind, Source.CLI, {}))
    assert main(["--home", str(tmp_path), "show", s.id]) == 0
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 8


def test_view_reports_port_in_use(tmp_path: Path, capsys):
    import socket
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        rc = main(["--home", str(tmp_path), "view", "--no-browser", "--port", str(port)])
    finally:
        blocker.close()
    assert rc == 1
    assert f"cannot listen on port {port}" in capsys.readouterr().err


def test_show_invalid_session_id_is_a_clean_error(tmp_path: Path, capsys):
    assert main(["--home", str(tmp_path), "show", "../../etc"]) == 1
    assert "invalid session id" in capsys.readouterr().err
    assert not (tmp_path / "blobs").exists()


# -- wire ----------------------------------------------------------------

def test_openai_chat_tool_message_with_content_parts_is_flattened():
    req = {"model": "gpt", "messages": [
        {"role": "tool", "tool_call_id": "c1",
         "content": [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}]},
        {"role": "tool", "tool_call_id": "c2", "content": None},
    ]}
    evs = extract("openai-chat", "/v1/chat/completions", json.dumps(req), "{}", 200, False,
                  Dedup(), 0, 1)
    results = [e for e in evs if e.kind == Kind.TOOL_RESULT]
    assert [r.payload["content"] for r in results] == ["line1\nline2", ""]


def test_openai_responses_function_output_parts_are_flattened():
    req = {"model": "gpt", "input": [
        {"type": "function_call_output", "call_id": "c1",
         "output": [{"type": "input_text", "text": "a"}, {"type": "input_image", "image_url": "x"}]},
    ]}
    evs = extract("openai-responses", "/v1/responses", json.dumps(req), "{}", 200, False,
                  Dedup(), 0, 1)
    (r,) = [e for e in evs if e.kind == Kind.TOOL_RESULT]
    assert r.payload["content"] == "a"
