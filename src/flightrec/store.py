"""On-disk session store: one directory per session.

    <root>/sessions/<session_id>/
        meta.json        harness command, cwd, start time
        events.jsonl     one Event per line, ordered by seq
        blobs/<sha256>   content-addressed file snapshots

Plain files on purpose: easy to inspect, diff, and ship to the viewer.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Iterator

from .events import Event, Kind, Source

DEFAULT_ROOT = Path(os.environ.get("FLIGHTREC_HOME", Path.home() / ".flightrec"))


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()


class BlobStore:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        target = self.dir / sha
        if not target.exists():
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        return sha

    def put_file(self, path: Path) -> str | None:
        try:
            return self.put(path.read_bytes())
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return None

    def get(self, sha: str) -> bytes:
        return (self.dir / sha).read_bytes()

    def has(self, sha: str) -> bool:
        return (self.dir / sha).exists()


class Session:
    """Append-only writer/reader for a single recording."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.blobs = BlobStore(self.dir / "blobs")
        self._events_path = self.dir / "events.jsonl"
        self._lock = threading.Lock()
        self._seq = self._count_existing()

    @property
    def id(self) -> str:
        return self.dir.name

    @property
    def meta_path(self) -> Path:
        return self.dir / "meta.json"

    def _count_existing(self) -> int:
        if not self._events_path.exists():
            return 0
        with self._events_path.open("rb") as f:
            return sum(1 for _ in f)

    # -- writing --------------------------------------------------------

    def write_meta(self, **meta) -> None:
        self.meta_path.write_text(json.dumps(meta, indent=2))

    def read_meta(self) -> dict:
        if self.meta_path.exists():
            return json.loads(self.meta_path.read_text())
        return {}

    def append(self, event: Event) -> Event:
        with self._lock:
            event.seq = self._seq
            self._seq += 1
            with self._events_path.open("a", encoding="utf-8") as f:
                f.write(event.to_json() + "\n")
        return event

    def start(self, command: list[str], cwd: str, **extra) -> None:
        self.write_meta(command=command, cwd=cwd, started=time.time(), **extra)
        self.append(Event(Kind.SESSION_START, Source.CLI, {"command": command, "cwd": cwd}))

    def end(self, exit_code: int | None = None) -> None:
        meta = self.read_meta()
        meta["ended"] = time.time()
        meta["exit_code"] = exit_code
        self.meta_path.write_text(json.dumps(meta, indent=2))
        self.append(Event(Kind.SESSION_END, Source.CLI, {"exit_code": exit_code}))

    # -- reading --------------------------------------------------------

    def events(self) -> Iterator[Event]:
        if not self._events_path.exists():
            return
        with self._events_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield Event.from_json(line)

    def __len__(self) -> int:
        return self._seq


def sessions_dir(root: Path = DEFAULT_ROOT) -> Path:
    d = root / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_session(root: Path = DEFAULT_ROOT) -> Session:
    return Session(sessions_dir(root) / new_session_id())


def open_session(session_id: str, root: Path = DEFAULT_ROOT) -> Session:
    d = sessions_dir(root) / session_id
    if not d.exists():
        raise FileNotFoundError(f"no session {session_id!r} under {root}")
    return Session(d)


def list_sessions(root: Path = DEFAULT_ROOT) -> list[Session]:
    return sorted((Session(d) for d in sessions_dir(root).iterdir() if d.is_dir()),
                  key=lambda s: s.id)
