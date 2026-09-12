"""Store: seq continuity across reopen, blank lines, listing, blobs."""

from pathlib import Path

import pytest

from flightrec.events import Event, Kind, Source
from flightrec.store import (BlobStore, Session, create_session, list_sessions,
                             new_session_id, open_session)


def test_seq_continues_after_reopen(tmp_path: Path):
    s = create_session(tmp_path)
    s.append(Event(Kind.NOTE, Source.CLI, {"n": 0}))
    s.append(Event(Kind.NOTE, Source.CLI, {"n": 1}))
    again = open_session(s.id, tmp_path)
    e = again.append(Event(Kind.NOTE, Source.CLI, {"n": 2}))
    assert e.seq == 2 and len(again) == 3
    assert [x.seq for x in again.events()] == [0, 1, 2]


def test_blank_lines_do_not_desync_seq(tmp_path: Path):
    s = create_session(tmp_path)
    s.append(Event(Kind.NOTE, Source.CLI))
    with (s.dir / "events.jsonl").open("a") as f:
        f.write("\n\n   \n")          # e.g. an interrupted write or manual edit
    again = open_session(s.id, tmp_path)
    assert len(again) == 1
    assert again.append(Event(Kind.NOTE, Source.CLI)).seq == 1
    assert [e.seq for e in again.events()] == [0, 1]


def test_events_on_fresh_session_is_empty(tmp_path: Path):
    s = create_session(tmp_path)
    assert list(s.events()) == [] and len(s) == 0 and s.read_meta() == {}


def test_open_missing_session_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        open_session("nope", tmp_path)


def test_list_sessions_ignores_stray_files_and_sorts(tmp_path: Path):
    a = create_session(tmp_path)
    b = create_session(tmp_path)
    (tmp_path / "sessions" / "README").write_text("not a session")
    ids = [s.id for s in list_sessions(tmp_path)]
    assert ids == sorted([a.id, b.id])


def test_new_session_id_is_time_sortable_and_unique():
    ids = {new_session_id() for _ in range(20)}
    assert len(ids) == 20
    for i in ids:
        assert len(i) == len("20260912-165443-ab12")


def test_start_and_end_update_meta(tmp_path: Path):
    s = create_session(tmp_path)
    s.start(["x"], cwd="/w", harness="x")
    meta = s.read_meta()
    assert meta["command"] == ["x"] and meta["harness"] == "x" and "started" in meta
    s.end(3)
    meta = s.read_meta()
    assert meta["exit_code"] == 3 and meta["ended"] >= meta["started"]
    assert [e.kind for e in s.events()] == [Kind.SESSION_START, Kind.SESSION_END]


def test_blob_store_put_file_skips_directories_and_has(tmp_path: Path):
    bs = BlobStore(tmp_path / "blobs")
    assert bs.put_file(tmp_path) is None            # a directory
    sha = bs.put(b"x")
    assert bs.has(sha) and not bs.has("0" * 64)
    assert not list((tmp_path / "blobs").glob("*.tmp"))   # temp file was renamed away


def test_session_id_is_directory_name(tmp_path: Path):
    s = Session(tmp_path / "custom-id")
    assert s.id == "custom-id" and s.dir.is_dir() and (s.dir / "blobs").is_dir()
