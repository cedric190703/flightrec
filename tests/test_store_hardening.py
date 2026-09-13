"""Store against untrusted names and damaged files: session ids, blob hashes,
corrupt meta.json, malformed event lines, atomic meta writes."""

import json
from pathlib import Path

import pytest

from flightrec.events import Event, Kind, Source
from flightrec.store import (BlobStore, Session, create_session, is_valid_session_id,
                             list_sessions, open_session)


@pytest.mark.parametrize("bad", ["..", ".", "", "../x", "a/b", "a\\b", "/abs", ".hidden",
                                 "x" * 129, "id with space", "id\n"])
def test_invalid_session_ids_rejected(bad):
    assert not is_valid_session_id(bad)


@pytest.mark.parametrize("good", ["20260914-101010-ab12", "abc", "A.b-c_d", "1"])
def test_valid_session_ids_accepted(good):
    assert is_valid_session_id(good)


def test_open_session_rejects_traversal_without_side_effects(tmp_path: Path):
    create_session(tmp_path)
    for evil in ["..", "../..", "sessions/../x", "/etc"]:
        with pytest.raises(FileNotFoundError):
            open_session(evil, tmp_path)
    # A rejected open never creates a stray blobs/ directory next to the sessions.
    assert not (tmp_path / "blobs").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sessions"]


def test_open_session_requires_directory(tmp_path: Path):
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "file-not-dir").write_text("x")
    with pytest.raises(FileNotFoundError):
        open_session("file-not-dir", tmp_path)


def test_blob_store_rejects_non_hash_names(tmp_path: Path):
    blobs = BlobStore(tmp_path / "blobs")
    (tmp_path / "secret.txt").write_text("SEKRET")
    sha = blobs.put(b"hello")
    assert blobs.has(sha) and blobs.get(sha) == b"hello"
    for bad in ["../secret.txt", "..", "", sha.upper(), sha[:63], sha + "0", "x" * 64]:
        assert blobs.has(bad) is False, bad
        with pytest.raises(FileNotFoundError):
            blobs.get(bad)


def test_read_meta_tolerates_missing_corrupt_and_non_object(tmp_path: Path):
    s = create_session(tmp_path)
    assert s.read_meta() == {}
    s.meta_path.write_text("{not json")
    assert s.read_meta() == {}
    s.meta_path.write_text("[1, 2, 3]")
    assert s.read_meta() == {}
    s.write_meta(command=["x"], cwd="/")
    assert s.read_meta()["command"] == ["x"]


def test_end_after_corrupt_meta_still_closes_session(tmp_path: Path):
    s = create_session(tmp_path)
    s.start(["cmd"], "/")
    s.meta_path.write_text("garbage")
    s.end(3)
    meta = s.read_meta()
    assert meta["exit_code"] == 3 and "ended" in meta
    assert [e.kind for e in s.events()] == [Kind.SESSION_START, Kind.SESSION_END]


def test_write_meta_is_atomic_and_leaves_no_temp_file(tmp_path: Path):
    s = create_session(tmp_path)
    s.write_meta(a=1)
    s.write_meta(a=2)
    assert json.loads(s.meta_path.read_text()) == {"a": 2}
    assert not list(s.dir.glob("meta.json.tmp"))


def test_list_sessions_survives_one_corrupt_session(tmp_path: Path):
    good = create_session(tmp_path)
    good.write_meta(command=["ok"], cwd="/")
    bad = create_session(tmp_path)
    bad.meta_path.write_text("{{{")
    ids = {s.id for s in list_sessions(tmp_path)}
    assert ids == {good.id, bad.id}
    assert all(isinstance(s.read_meta(), dict) for s in list_sessions(tmp_path))


def test_events_skip_malformed_lines_and_keep_seq(tmp_path: Path):
    s = create_session(tmp_path)
    s.append(Event(Kind.NOTE, Source.CLI, {"n": 0}))
    s.append(Event(Kind.NOTE, Source.CLI, {"n": 1}))
    with (s.dir / "events.jsonl").open("a") as f:
        f.write('{"kind": "note", "source": "cli", "payload": {}, "seq": 2, "truncated": tr')  # no newline
    # Reopen: the partial line is counted, so the next seq does not collide.
    s2 = Session(s.dir)
    assert len(s2) == 3
    s2.append(Event(Kind.NOTE, Source.CLI, {"n": 3}))

    evs = list(s2.events())
    assert [e.payload["n"] for e in evs] == [0, 1, 3]
    assert [e.seq for e in evs] == [0, 1, 3]


def test_events_strict_mode_raises_with_line_number(tmp_path: Path):
    s = create_session(tmp_path)
    s.append(Event(Kind.NOTE, Source.CLI, {}))
    with (s.dir / "events.jsonl").open("a") as f:
        f.write("[1, 2]\n")           # valid JSON, but not an event object
        f.write('{"kind": 1}\n')      # object with a wrong-typed field
    assert len(list(s.events())) == 1
    with pytest.raises(ValueError, match="events.jsonl:2"):
        list(s.events(strict=True))


def test_events_tolerates_invalid_utf8(tmp_path: Path):
    s = create_session(tmp_path)
    s.append(Event(Kind.NOTE, Source.CLI, {"ok": True}))
    with (s.dir / "events.jsonl").open("ab") as f:
        f.write(b'{"kind":"note","source":"cli","payload":{"t":"\xff\xfe"}}\n')
    evs = list(s.events())
    assert len(evs) == 2 and "�" in evs[1].payload["t"]
