from pathlib import Path

from flightrec.events import Event, Kind, Snapshot, Source
from flightrec.store import create_session, list_sessions, open_session


def test_event_roundtrip():
    e = Event(Kind.FS_CHANGE, Source.FS, {"path": "a.py"},
              snapshots=[Snapshot("a.py", None, "abc")])
    back = Event.from_json(e.to_json())
    assert back.kind == "fs_change"
    assert back.snapshots[0].after == "abc"
    assert back.id == e.id


def test_session_append_and_read(tmp_path: Path):
    s = create_session(tmp_path)
    s.start(["echo", "hi"], cwd="/x")
    s.append(Event(Kind.NOTE, Source.CLI, {"msg": "hello"}))
    s.end(0)

    evs = list(s.events())
    assert [e.seq for e in evs] == [0, 1, 2]
    assert evs[0].kind == Kind.SESSION_START
    assert evs[-1].payload["exit_code"] == 0
    assert s.read_meta()["exit_code"] == 0

    reopened = open_session(s.id, tmp_path)
    assert len(reopened) == 3
    assert [x.id for x in list_sessions(tmp_path)] == [s.id]


def test_blob_store_dedup(tmp_path: Path):
    s = create_session(tmp_path)
    a = s.blobs.put(b"same")
    b = s.blobs.put(b"same")
    assert a == b and s.blobs.get(a) == b"same"
    assert s.blobs.put_file(tmp_path / "missing") is None
