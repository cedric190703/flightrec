"""Watcher against races and odd trees: files vanishing mid-emit, unreadable
paths, dot-directories at the root, and a flusher that must outlive errors."""

import os
import threading
import time
from pathlib import Path

import flightrec.fswatch as fswatch
from flightrec.events import Kind, Source
from flightrec.fswatch import FsWatcher
from flightrec.store import create_session


def _fs_events(session):
    return [e for e in session.events() if e.kind == Kind.FS_CHANGE]


def _watcher(tmp_path, ignores=None):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    w = FsWatcher(proj, create_session(tmp_path / "home"), ignores)
    w._seed_index()
    return proj, w


def test_seed_does_not_descend_into_dot_directories_at_root(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    for d in [".git/objects/ab", ".flightrec/sessions/x", "src/.git/refs", "node_modules/p"]:
        (proj / d).mkdir(parents=True)
        (proj / d / "f").write_text("x")
    (proj / "src" / "a.py").write_text("a")

    visited: list[str] = []
    real_walk = os.walk

    def spying_walk(top, *a, **k):
        for entry in real_walk(top, *a, **k):
            visited.append(Path(entry[0]).relative_to(proj).as_posix())
            yield entry

    monkeypatch.setattr(fswatch.os, "walk", spying_walk)
    w = FsWatcher(proj, create_session(tmp_path / "home"))
    w._seed_index()
    assert set(w._index) == {"src/a.py"}
    # The prune must happen at the directory level, not only per file.
    assert set(visited) == {".", "src"}, visited


def test_file_deleted_between_stat_and_snapshot_is_recorded_as_delete(tmp_path, monkeypatch):
    proj, w = _watcher(tmp_path)
    (proj / "a.txt").write_text("v1")
    w.schedule(proj / "a.txt")
    w._flush(force=True)
    assert [e.payload["op"] for e in _fs_events(w.session)] == ["create"]

    # Simulate the file vanishing right after is_file() said it exists.
    real_is_file = Path.is_file

    def racy_is_file(self):
        r = real_is_file(self)
        if self.name == "a.txt":
            self.unlink()
        return r

    monkeypatch.setattr(Path, "is_file", racy_is_file)
    w.schedule(proj / "a.txt")
    w._flush(force=True)
    ops = [e.payload["op"] for e in _fs_events(w.session)]
    assert ops == ["create", "delete"]
    assert _fs_events(w.session)[-1].payload["size"] == 0


def test_rel_survives_paths_that_cannot_be_resolved(tmp_path, monkeypatch):
    proj, w = _watcher(tmp_path)
    (proj / "ok.txt").write_text("1")
    assert w._rel(proj / "ok.txt") == "ok.txt"

    def failing_resolve(self, strict=False):
        raise OSError("too many levels of symbolic links")

    monkeypatch.setattr(Path, "resolve", failing_resolve)
    assert w._rel(proj / "loop" / "x") is None   # no exception, just not tracked
    w.schedule(proj / "loop" / "x")
    assert w._pending == {}


def test_snapshot_of_path_through_a_file_returns_none(tmp_path):
    proj, w = _watcher(tmp_path)
    (proj / "f").write_text("x")
    assert w._snapshot(proj / "f" / "nope") is None
    assert w._snapshot(proj / "missing") is None


def test_flusher_thread_survives_an_exception_and_notes_it(tmp_path, monkeypatch):
    proj, w = _watcher(tmp_path)
    calls = {"n": 0}
    real_emit = w._emit

    def flaky_emit(rel, ts=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("synthetic failure")
        return real_emit(rel, ts)

    monkeypatch.setattr(w, "_emit", flaky_emit)
    monkeypatch.setattr(fswatch, "DEBOUNCE_S", 0.0)
    w._flusher.start()
    try:
        (proj / "one.txt").write_text("1")
        w.schedule(proj / "one.txt")
        deadline = time.time() + 2
        while calls["n"] < 1 and time.time() < deadline:
            time.sleep(0.02)
        (proj / "two.txt").write_text("2")
        w.schedule(proj / "two.txt")
        while calls["n"] < 2 and time.time() < deadline:
            time.sleep(0.02)
    finally:
        w._stop.set()
        w._flusher.join(timeout=2)
    assert not w._flusher.is_alive()
    kinds = [(e.kind, e.source) for e in w.session.events()]
    assert (Kind.NOTE, Source.FS) in kinds
    assert [e.payload["path"] for e in _fs_events(w.session)] == ["two.txt"]
    note = next(e for e in w.session.events() if e.kind == Kind.NOTE)
    assert "synthetic failure" in note.payload["error"]


def test_note_swallows_unwritable_log(tmp_path, monkeypatch):
    proj, w = _watcher(tmp_path)
    monkeypatch.setattr(w.session, "append", lambda ev: (_ for _ in ()).throw(OSError("disk full")))
    w._note("x")  # must not raise


def test_stop_flushes_pending_even_if_never_started(tmp_path):
    proj, w = _watcher(tmp_path)
    (proj / "late.txt").write_text("z")
    w.schedule(proj / "late.txt")
    w.stop()
    assert [e.payload["path"] for e in _fs_events(w.session)] == ["late.txt"]
    assert not w._flusher.is_alive()
    t = threading.Thread(target=w.stop)   # second stop from another thread is a no-op
    t.start()
    t.join(timeout=2)
    assert not t.is_alive()
