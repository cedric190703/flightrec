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
import re
import threading
import time
from pathlib import Path
from typing import Iterator

from .events import Event, Kind, Source

DEFAULT_ROOT = Path(os.environ.get("FLIGHTREC_HOME", Path.home() / ".flightrec"))

# Session ids and blob hashes come from URLs and CLI arguments, and both are
# joined onto paths; anything that is not a plain name must be rejected.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()


def is_valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.fullmatch(session_id))


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + rename so a crash never leaves a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
        if not _SHA256_RE.fullmatch(sha):
            raise FileNotFoundError(f"not a blob hash: {sha[:80]!r}")
        return (self.dir / sha).read_bytes()

    def has(self, sha: str) -> bool:
        return bool(_SHA256_RE.fullmatch(sha)) and (self.dir / sha).is_file()


class Session:
    """Append-only writer/reader for a single recording."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.blobs = BlobStore(self.dir / "blobs")
        self._events_path = self.dir / "events.jsonl"
        self._lock = threading.Lock()
        self._unterminated = False   # last line lacks "\n" (crash mid-write)
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
        # Keep a slot for every nonblank line, including damaged records, but
        # also honor stored sequences: edited logs can have gaps or be reordered.
        n = 0
        next_seq = 0
        last = b"\n"
        with self._events_path.open("rb") as f:
            for last in f:
                if not last.strip():
                    continue
                n += 1
                try:
                    record = json.loads(last.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                seq = record.get("seq") if isinstance(record, dict) else None
                # bool is an int subclass, but is not an event sequence.
                if type(seq) is int and seq >= 0:
                    next_seq = max(next_seq, seq + 1)
        # A partial final line would swallow the next append; terminate it first.
        self._unterminated = not last.endswith(b"\n")
        return max(n, next_seq)

    # -- writing --------------------------------------------------------

    def write_meta(self, **meta) -> None:
        _atomic_write_text(self.meta_path, json.dumps(meta, indent=2))

    def read_meta(self) -> dict:
        """The session's metadata, or ``{}`` if the file is missing or damaged.

        A single unreadable session must not break ``list`` or the viewer.
        """
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return meta if isinstance(meta, dict) else {}

    def append(self, event: Event) -> Event:
        with self._lock:
            event.seq = self._seq
            self._seq += 1
            with self._events_path.open("a", encoding="utf-8") as f:
                if self._unterminated:
                    f.write("\n")
                    self._unterminated = False
                f.write(event.to_json() + "\n")
        return event

    def start(self, command: list[str], cwd: str, **extra) -> None:
        self.write_meta(command=command, cwd=cwd, started=time.time(), **extra)
        self.append(Event(Kind.SESSION_START, Source.CLI, {"command": command, "cwd": cwd}))

    def end(self, exit_code: int | None = None) -> None:
        meta = self.read_meta()
        meta["ended"] = time.time()
        meta["exit_code"] = exit_code
        self.write_meta(**meta)
        self.append(Event(Kind.SESSION_END, Source.CLI, {"exit_code": exit_code}))

    # -- reading --------------------------------------------------------

    def events(self, strict: bool = False) -> Iterator[Event]:
        """Yield the recorded events in file order.

        Lines that fail to parse (a crash mid-write, a foreign edit) are
        skipped so the rest of the recording stays usable; pass ``strict=True``
        to raise ``ValueError`` on the first bad line instead. Each event
        carries its own ``seq``, so skipping never renumbers the survivors.
        """
        if not self._events_path.exists():
            return
        with self._events_path.open(encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield Event.from_json(line)
                except (ValueError, TypeError) as exc:
                    if strict:
                        raise ValueError(f"{self._events_path}:{lineno}: {exc}") from exc

    def __len__(self) -> int:
        return self._seq


def sessions_dir(root: Path = DEFAULT_ROOT) -> Path:
    d = root / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_session(root: Path = DEFAULT_ROOT) -> Session:
    return Session(sessions_dir(root) / new_session_id())


def open_session(session_id: str, root: Path = DEFAULT_ROOT) -> Session:
    if not is_valid_session_id(session_id):
        raise FileNotFoundError(f"invalid session id {session_id[:80]!r}")
    d = sessions_dir(root) / session_id
    if not d.is_dir():
        raise FileNotFoundError(f"no session {session_id!r} under {root}")
    return Session(d)


def list_sessions(root: Path = DEFAULT_ROOT) -> list[Session]:
    return sorted((Session(d) for d in sessions_dir(root).iterdir() if d.is_dir()),
                  key=lambda s: s.id)
