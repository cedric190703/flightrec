"""Exec observer: PATH shims that log every shell command the agent runs.

We generate a temporary directory of tiny POSIX ``sh`` wrappers named like
common shells and dev tools (bash, git, npm, pytest, ...) and prepend it to
``PATH`` before launching the harness. Each wrapper logs the command, runs
the real binary with stdio passed through untouched, then logs the exit
code. Nested invocations (a ``git`` launched from a shimmed ``bash -c``)
are suppressed so each agent action is recorded once, at the top level.

Limitation (documented): commands started via an absolute path such as
``/bin/sh -c`` bypass PATH and are not seen. The filesystem watcher still
catches their side effects.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

from .events import Event, Kind, Source
from .store import Session

SHELLS = ["sh", "bash", "zsh", "fish", "dash"]
TOOLS = [
    "git", "gh",
    "npm", "npx", "pnpm", "yarn", "bun", "node", "deno",
    "python", "python3", "pip", "pip3", "uv", "pytest", "poetry",
    "make", "cmake", "cargo", "go", "rustc", "gcc", "clang",
    "ruby", "bundle", "java", "mvn", "gradle",
    "docker", "kubectl", "curl", "wget",
]

_TEMPLATE = """#!/bin/sh
# flightrec shim for {name}
_real={real}
if [ -z "$FLIGHTREC_EXEC_LOG" ] || [ -n "$FLIGHTREC_IN_SHIM" ]; then
  exec "$_real" "$@"
fi
_id=$("$FLIGHTREC_PYTHON" -m flightrec.shimlog start "$_real" "$@")
FLIGHTREC_IN_SHIM=1 "$_real" "$@"
_rc=$?
"$FLIGHTREC_PYTHON" -m flightrec.shimlog end "$_id" "$_rc"
exit $_rc
"""


def _which(name: str, path: str) -> str | None:
    return shutil.which(name, path=path)


class ShimDir:
    """Creates the shim directory and the env vars needed to activate it."""

    def __init__(self, session: Session, extra_tools: list[str] | None = None):
        self.session = session
        self.dir = Path(tempfile.mkdtemp(prefix="flightrec-shim-"))
        self.log_path = session.dir / "exec.jsonl"
        self.names = SHELLS + TOOLS + (extra_tools or [])
        self.shimmed: dict[str, str] = {}

    def build(self, base_path: str | None = None) -> None:
        base_path = base_path or os.environ.get("PATH", "")
        # Never resolve to ourselves if a previous shim dir is still on PATH.
        clean = os.pathsep.join(p for p in base_path.split(os.pathsep)
                                if "flightrec-shim-" not in p)
        for name in self.names:
            real = _which(name, clean)
            if not real:
                continue
            script = self.dir / name
            script.write_text(_TEMPLATE.format(name=name, real=shlex.quote(real)))
            script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            self.shimmed[name] = real

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(base if base is not None else os.environ)
        env["PATH"] = str(self.dir) + os.pathsep + env.get("PATH", "")
        env["FLIGHTREC_EXEC_LOG"] = str(self.log_path)
        env["FLIGHTREC_PYTHON"] = sys.executable
        env.pop("FLIGHTREC_IN_SHIM", None)
        return env

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class ExecCollector:
    """Tails exec.jsonl and turns start/end pairs into ``exec`` events."""

    def __init__(self, session: Session, log_path: Path):
        self.session = session
        self.log_path = log_path
        self._open: dict[str, Event] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._pos = 0

    def start(self) -> None:
        if self._thread.is_alive():
            return
        if self._stop.is_set():
            raise RuntimeError("cannot restart a stopped ExecCollector")
        self._thread.start()

    def stop(self) -> None:
        if not self._thread.is_alive():
            return
        self._stop.set()
        self._thread.join(timeout=2)
        self.drain()

    def _loop(self) -> None:
        while not self._stop.wait(0.1):
            try:
                self.drain()
            except Exception as exc:  # noqa: BLE001 - keep tailing after one bad record
                self.session.append(Event(Kind.NOTE, Source.SHIM, {"error": f"exec log: {exc!r}"}))

    def drain(self) -> None:
        if not self.log_path.exists():
            return
        # Binary mode: seeking a text stream to a byte offset is undefined.
        with self.log_path.open("rb") as f:
            f.seek(self._pos)
            for line in f:
                if not line.endswith(b"\n"):
                    break  # partial write, retry next tick
                self._pos += len(line)
                try:
                    rec = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    self._handle(rec)

    def _handle(self, rec: dict) -> None:
        rec_id = rec.get("id")
        if not isinstance(rec_id, str) or not rec_id:
            return  # the shim could not obtain an id; nothing to pair
        ts = _timestamp(rec.get("ts"))
        if rec.get("phase") == "start":
            argv = [str(a) for a in rec.get("argv") or []] if isinstance(rec.get("argv"), list) else []
            real = str(rec.get("real") or "")
            ev = Event(Kind.EXEC, Source.SHIM, {
                "argv": argv, "real": real, "cwd": rec.get("cwd"),
                "command": " ".join([os.path.basename(real), *argv]),
                "exit_code": None, "duration": None,
            }, ts=ts)
            self._open[rec_id] = ev
            self.session.append(ev)
        elif rec.get("phase") == "end":
            ev = self._open.pop(rec_id, None)
            if ev is None:
                return
            exit_code = rec.get("exit_code")
            # Emit completion as a separate linked event; the log is append-only.
            self.session.append(Event(Kind.EXEC, Source.SHIM, {
                "phase": "end", "exit_code": exit_code if isinstance(exit_code, int) else None,
                "duration": round(max(ts - ev.ts, 0.0), 3), "command": ev.payload["command"],
            }, ts=ts, links=[ev.id]))


def _timestamp(v: object) -> float:
    """A wall-clock time from an untrusted log field, or now."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return float(v)
    return time.time()
