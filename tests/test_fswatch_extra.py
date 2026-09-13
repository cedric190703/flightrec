"""Watcher internals: timestamps, debounce, ignores, size cap, out-of-root paths."""

import time
from pathlib import Path

import pytest

import flightrec.fswatch as fswatch
from flightrec.events import Kind
from flightrec.fswatch import DEFAULT_IGNORES, FsWatcher, _is_ignored
from flightrec.store import create_session


def _fs_events(session):
    return [e for e in session.events() if e.kind == Kind.FS_CHANGE]


def _watcher(tmp_path, ignores=None):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    w = FsWatcher(proj, create_session(tmp_path / "home"), ignores)
    w._seed_index()          # no observer thread: we drive schedule/_flush by hand
    return proj, w


def test_event_ts_is_first_observation_not_flush_time(tmp_path):
    proj, w = _watcher(tmp_path)
    (proj / "a.txt").write_text("1")
    t_seen = time.time()
    w.schedule(proj / "a.txt")
    time.sleep(0.3)                          # longer than DEBOUNCE_S
    (proj / "a.txt").write_text("12")
    w.schedule(proj / "a.txt")               # re-scheduling keeps the first-seen time
    t_flush = time.time()
    w._flush(force=True)
    (ev,) = _fs_events(w.session)
    assert abs(ev.ts - t_seen) < 0.05
    assert ev.ts < t_flush - 0.25
    assert w.session.blobs.get(ev.snapshots[0].after) == b"12"   # content is the latest, though


def test_debounce_holds_until_deadline(tmp_path):
    proj, w = _watcher(tmp_path)
    (proj / "a.txt").write_text("1")
    w.schedule(proj / "a.txt")
    w._flush()                               # not due yet
    assert _fs_events(w.session) == []
    time.sleep(fswatch.DEBOUNCE_S + 0.05)
    w._flush()
    assert len(_fs_events(w.session)) == 1
    assert w._pending == {}


def test_noop_write_emits_nothing(tmp_path):
    proj, w = _watcher(tmp_path)
    (proj / "pre.txt").write_text("same")
    w._seed_index()
    (proj / "pre.txt").write_text("same")    # touch with identical content
    w.schedule(proj / "pre.txt")
    w._flush(force=True)
    assert _fs_events(w.session) == []


def test_extra_ignore_patterns_and_paths_outside_root(tmp_path):
    proj, w = _watcher(tmp_path, ignores=["*.log", "secrets"])
    assert w._rel(proj / "app.log") is None
    assert w._rel(proj / "secrets" / "k.pem") is None
    assert w._rel(proj / "src" / "m.py") == "src/m.py"
    assert w._rel(tmp_path / "elsewhere.py") is None       # not under root
    w.schedule(proj / "app.log")
    assert w._pending == {}


def test_seed_index_prunes_ignored_directories(tmp_path):
    proj = tmp_path / "proj"
    (proj / "node_modules" / "x").mkdir(parents=True)
    (proj / "node_modules" / "x" / "i.js").write_text("x")
    (proj / ".git").mkdir()
    (proj / ".git" / "HEAD").write_text("ref")
    (proj / "src").mkdir()
    (proj / "src" / "a.py").write_text("a")
    w = FsWatcher(proj, create_session(tmp_path / "home"))
    w._seed_index()
    assert set(w._index) == {"src/a.py"}


def test_oversized_file_snapshot_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(fswatch, "MAX_SNAPSHOT_BYTES", 10)
    proj, w = _watcher(tmp_path)
    (proj / "big.bin").write_bytes(b"x" * 100)
    w.schedule(proj / "big.bin")
    w._flush(force=True)
    # No content could be captured, so before == after (None) and nothing is emitted…
    assert _fs_events(w.session) == []
    # …but a file shrinking under the cap is recorded as a create from "unknown".
    (proj / "big.bin").write_bytes(b"tiny")
    w.schedule(proj / "big.bin")
    w._flush(force=True)
    (ev,) = _fs_events(w.session)
    assert ev.payload["op"] == "create" and ev.payload["size"] == 4


def test_default_ignores_cover_common_noise():
    for rel in ["a/b/node_modules/c/d.js", ".venv/lib/x.py", "x/__pycache__/y.pyc",
                "dist/bundle.js", "pkg.egg-info/PKG-INFO", ".DS_Store", "notes.txt~",
                ".flightrec/sessions/x/events.jsonl"]:
        assert _is_ignored(rel, DEFAULT_IGNORES), rel
    for rel in ["src/app.py", "README.md", "tests/test_x.py", "distribution.md"]:
        assert not _is_ignored(rel, DEFAULT_IGNORES), rel


def test_macos_python_314_uses_polling_observer(monkeypatch):
    monkeypatch.setattr(fswatch.sys, "platform", "darwin")
    monkeypatch.setattr(fswatch.sys, "version_info", (3, 14))
    assert isinstance(fswatch._default_observer(), fswatch.PollingObserver)


def test_watcher_stop_is_idempotent_and_prevents_restart(tmp_path):
    class FakeObserver:
        def __init__(self):
            self.alive = False
            self.scheduled = 0

        def schedule(self, *_args, **_kwargs):
            self.scheduled += 1

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def stop(self):
            self.alive = False

        def join(self, timeout):
            assert timeout == 2

    proj = tmp_path / "proj"
    proj.mkdir()
    observer = FakeObserver()
    w = FsWatcher(proj, create_session(tmp_path / "home"), observer_factory=lambda: observer)
    w.start()
    w.stop()
    w.stop()
    assert observer.scheduled == 1
    with pytest.raises(RuntimeError, match="cannot restart"):
        w.start()


def test_live_watcher_records_rename_as_delete_plus_create(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "old.txt").write_text("v")
    session = create_session(tmp_path / "home")
    w = FsWatcher(proj, session)
    w.start()
    try:
        time.sleep(0.3)
        (proj / "old.txt").rename(proj / "new.txt")
        deadline = time.time() + 8
        while time.time() < deadline and len(_fs_events(session)) < 2:
            time.sleep(0.05)
    finally:
        w.stop()
    ops = {(e.payload["path"], e.payload["op"]) for e in _fs_events(session)}
    assert ops == {("old.txt", "delete"), ("new.txt", "create")}
