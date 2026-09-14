"""Filesystem observer: records every file change under the project root.

For each change we store a content snapshot (before/after) in the session's
blob store so the viewer can reconstruct the working tree at any step.

"Before" content comes from an in-memory index of the last-seen hash per
path, seeded by a scan at startup; so the very first edit to a file still
gets a correct ``before``.
"""

from __future__ import annotations

import fnmatch
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver
from watchdog.observers.polling import PollingObserver

from .events import Event, Kind, Snapshot, Source
from .store import Session

DEFAULT_IGNORES = [
    ".git", ".git/*", "*/.git/*",
    "node_modules", "*/node_modules/*",
    ".venv", "venv", "*/.venv/*", "*/venv/*",
    "__pycache__", "*/__pycache__/*", "*.pyc",
    ".flightrec", "*/.flightrec/*",
    ".DS_Store", "*.swp", "*.tmp", "*~",
    ".pytest_cache", "*/.pytest_cache/*",
    "dist", "build", "*.egg-info",
    # Credentials: never snapshot these into the blob store. Override with an
    # explicit --ignore only-list is not possible, but they can be recovered
    # from the user's own filesystem; a recording must stay safe to share.
    ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*.p12", "*.pfx",
    ".npmrc", ".netrc", ".pypirc",
]

# Editors and harnesses often write a file several times within a few ms
# (truncate, write, chmod). Coalesce changes to the same path inside this window.
DEBOUNCE_S = 0.15
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024


def _default_observer() -> BaseObserver:
    """Return the most reliable observer for the current runtime.

    Watchdog's native macOS FSEvents extension has crashed on some Python 3.14
    builds. Polling is less efficient, but keeps recording functional instead
    of risking a process crash while upstream support catches up.
    """
    if sys.platform == "darwin" and sys.version_info >= (3, 14):
        return PollingObserver(timeout=0.1)
    return Observer()


def _is_ignored(rel: str, patterns: list[str]) -> bool:
    parts = rel.split("/")
    for p in patterns:
        if fnmatch.fnmatch(rel, p) or any(fnmatch.fnmatch(part, p) for part in parts):
            return True
    return False


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "FsWatcher"):
        self.w = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        paths = [event.src_path]
        dest = getattr(event, "dest_path", None)
        if dest:
            paths.append(dest)
        for p in paths:
            self.w.schedule(Path(os.fsdecode(p)))


class FsWatcher:
    def __init__(self, root: Path, session: Session, ignores: list[str] | None = None,
                 observer_factory: Callable[[], BaseObserver] | None = None):
        self.root = root.resolve()
        self.session = session
        self.ignores = DEFAULT_IGNORES + (ignores or [])
        self._index: dict[str, str | None] = {}     # rel path -> last known sha
        # rel path -> (debounce deadline, wall-clock time the change was first seen)
        self._pending: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._observer = (observer_factory or _default_observer)()
        self._flusher = threading.Thread(target=self._flush_loop, daemon=True)
        self._running = False
        self._closed = False

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        if self._closed:
            raise RuntimeError("cannot restart a stopped FsWatcher")
        self._seed_index()
        self._observer.schedule(_Handler(self), str(self.root), recursive=True)
        try:
            self._observer.start()
            self._flusher.start()
        except Exception:
            if self._observer.is_alive():
                self._observer.stop()
                self._observer.join(timeout=2)
            raise
        self._running = True

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._observer.is_alive():
            self._observer.stop()
            self._observer.join(timeout=2)
        if self._flusher.is_alive():
            self._flusher.join(timeout=2)
        self._flush(force=True)
        self._running = False

    # -- internals ------------------------------------------------------

    def _rel(self, path: Path) -> str | None:
        try:
            rel = path.resolve().relative_to(self.root).as_posix()
        except (ValueError, OSError, RuntimeError):  # outside root, vanished, symlink loop
            return None
        return None if _is_ignored(rel, self.ignores) else rel

    def _seed_index(self) -> None:
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = Path(dirpath).relative_to(self.root).as_posix()
            # ``rel_dir`` is "." at the root; a naive f"{rel_dir}/{d}".lstrip("./")
            # would also strip the leading dot of ".git" and defeat the prune.
            prefix = "" if rel_dir == "." else rel_dir + "/"
            dirnames[:] = [d for d in dirnames if not _is_ignored(prefix + d, self.ignores)]
            for fn in filenames:
                p = Path(dirpath) / fn
                rel = self._rel(p)
                if rel is not None:
                    self._index[rel] = self._snapshot(p)

    def _snapshot(self, path: Path) -> str | None:
        try:
            if path.stat().st_size > MAX_SNAPSHOT_BYTES:
                return None
        except OSError:  # vanished, unreadable, or a path component is not a dir
            return None
        return self.session.blobs.put_file(path)

    def schedule(self, path: Path) -> None:
        rel = self._rel(path)
        if rel is None:
            return
        with self._lock:
            first_seen = self._pending[rel][1] if rel in self._pending else time.time()
            self._pending[rel] = (time.monotonic() + DEBOUNCE_S, first_seen)

    def _flush_loop(self) -> None:
        while not self._stop.wait(0.05):
            try:
                self._flush()
            except Exception as exc:  # noqa: BLE001 - one bad path must not end the watch
                self._note(f"fs flush failed: {exc!r}")

    def _note(self, error: str) -> None:
        try:
            self.session.append(Event(Kind.NOTE, Source.FS, {"error": error}))
        except Exception:  # noqa: BLE001 - the log itself is unwritable; nothing left to do
            pass

    def _flush(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            due = [(r, seen) for r, (deadline, seen) in self._pending.items()
                   if force or deadline <= now]
            for r, _ in due:
                del self._pending[r]
        for rel, seen in due:
            self._emit(rel, seen)

    def _emit(self, rel: str, ts: float | None = None) -> None:
        """Record the current state of ``rel`` if it differs from the last known one.

        ``ts`` is when the change was first observed, not when the debounce
        fired: the correlator matches fs events against tool_call/tool_result
        timestamps, so a 150 ms debounce delay must not leak into the log.
        """
        path = self.root / rel
        before = self._index.get(rel)
        try:
            size = path.stat().st_size if path.is_file() else None
        except OSError:  # deleted between the two calls, or unreadable
            size = None
        after = self._snapshot(path) if size is not None else None
        if before == after:
            return  # touch / no-op write
        self._index[rel] = after
        op = "create" if before is None else "delete" if after is None else "modify"
        self.session.append(Event(
            Kind.FS_CHANGE, Source.FS,
            {"path": rel, "op": op, "size": size or 0},
            ts=ts if ts is not None else time.time(),
            snapshots=[Snapshot(rel, before, after)],
        ))
