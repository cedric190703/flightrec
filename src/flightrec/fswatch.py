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
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

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
]

# Editors and harnesses often write a file several times within a few ms
# (truncate, write, chmod). Coalesce changes to the same path inside this window.
DEBOUNCE_S = 0.15
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024


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
    def __init__(self, root: Path, session: Session, ignores: list[str] | None = None):
        self.root = root.resolve()
        self.session = session
        self.ignores = DEFAULT_IGNORES + (ignores or [])
        self._index: dict[str, str | None] = {}     # rel path -> last known sha
        self._pending: dict[str, float] = {}        # rel path -> deadline
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._observer = Observer()
        self._flusher = threading.Thread(target=self._flush_loop, daemon=True)

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self._seed_index()
        self._observer.schedule(_Handler(self), str(self.root), recursive=True)
        self._observer.start()
        self._flusher.start()

    def stop(self) -> None:
        self._stop.set()
        self._observer.stop()
        self._observer.join(timeout=2)
        self._flusher.join(timeout=2)
        self._flush(force=True)

    # -- internals ------------------------------------------------------

    def _rel(self, path: Path) -> str | None:
        try:
            rel = path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None
        return None if _is_ignored(rel, self.ignores) else rel

    def _seed_index(self) -> None:
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = Path(dirpath).relative_to(self.root).as_posix()
            dirnames[:] = [d for d in dirnames
                           if not _is_ignored(f"{rel_dir}/{d}".lstrip("./"), self.ignores)]
            for fn in filenames:
                p = Path(dirpath) / fn
                rel = self._rel(p)
                if rel is not None:
                    self._index[rel] = self._snapshot(p)

    def _snapshot(self, path: Path) -> str | None:
        try:
            if path.stat().st_size > MAX_SNAPSHOT_BYTES:
                return None
        except FileNotFoundError:
            return None
        return self.session.blobs.put_file(path)

    def schedule(self, path: Path) -> None:
        rel = self._rel(path)
        if rel is None:
            return
        with self._lock:
            self._pending[rel] = time.monotonic() + DEBOUNCE_S

    def _flush_loop(self) -> None:
        while not self._stop.wait(0.05):
            self._flush()

    def _flush(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            due = [r for r, t in self._pending.items() if force or t <= now]
            for r in due:
                del self._pending[r]
        for rel in due:
            self._emit(rel)

    def _emit(self, rel: str) -> None:
        path = self.root / rel
        before = self._index.get(rel)
        exists = path.is_file()
        after = self._snapshot(path) if exists else None
        if before == after:
            return  # touch / no-op write
        self._index[rel] = after
        op = "create" if before is None else "delete" if after is None else "modify"
        size = path.stat().st_size if exists else 0
        self.session.append(Event(
            Kind.FS_CHANGE, Source.FS,
            {"path": rel, "op": op, "size": size},
            snapshots=[Snapshot(rel, before, after)],
        ))
