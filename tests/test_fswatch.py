import time
from pathlib import Path

from flightrec.events import Kind
from flightrec.fswatch import FsWatcher, _is_ignored, DEFAULT_IGNORES
from flightrec.store import create_session


def _wait_for(session, n, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        evs = [e for e in session.events() if e.kind == Kind.FS_CHANGE]
        if len(evs) >= n:
            return evs
        time.sleep(0.05)
    return [e for e in session.events() if e.kind == Kind.FS_CHANGE]


def test_ignores():
    assert _is_ignored(".git/HEAD", DEFAULT_IGNORES)
    assert _is_ignored("pkg/__pycache__/x.pyc", DEFAULT_IGNORES)
    assert not _is_ignored("src/main.py", DEFAULT_IGNORES)


def test_watch_create_modify_delete(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pre.txt").write_text("v1")
    session = create_session(tmp_path / "home")

    w = FsWatcher(proj, session)
    w.start()
    try:
        time.sleep(0.3)
        (proj / "new.txt").write_text("hello")
        evs = _wait_for(session, 1)
        (proj / "pre.txt").write_text("v2")
        evs = _wait_for(session, 2)
        (proj / "new.txt").unlink()
        evs = _wait_for(session, 3)
    finally:
        w.stop()

    by_path = {(e.payload["path"], e.payload["op"]): e for e in evs}
    create = by_path[("new.txt", "create")]
    assert create.snapshots[0].before is None
    assert session.blobs.get(create.snapshots[0].after) == b"hello"

    modify = by_path[("pre.txt", "modify")]
    assert session.blobs.get(modify.snapshots[0].before) == b"v1"
    assert session.blobs.get(modify.snapshots[0].after) == b"v2"

    delete = by_path[("new.txt", "delete")]
    assert delete.snapshots[0].after is None
