"""Orchestrates all observers around a single harness process."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from .events import Event, Kind, Source
from .fswatch import FsWatcher
from .proxy import LlmProxy
from .shim import ExecCollector, ShimDir
from .store import Session, create_session


class Recorder:
    def __init__(self, command: list[str], cwd: Path, root: Path | None = None,
                 ignores: list[str] | None = None, harness: str | None = None,
                 proxy: bool = True):
        self.command = command
        self.cwd = cwd.resolve()
        self.session: Session = create_session(root) if root else create_session()
        self.harness = harness or Path(command[0]).name
        self._fs = FsWatcher(self.cwd, self.session, ignores)
        self._shim = ShimDir(self.session)
        self._exec = ExecCollector(self.session, self._shim.log_path)
        self._proxy = LlmProxy(self.session) if proxy else None
        self._proc: subprocess.Popen | None = None

    def _env(self) -> dict[str, str]:
        env = self._shim.env()
        if self._proxy:
            env = self._proxy.env(env)
        env["FLIGHTREC_SESSION"] = self.session.id
        return env

    def run(self) -> int:
        self.session.start(self.command, str(self.cwd), harness=self.harness)
        self._shim.build()
        self._fs.start()
        self._exec.start()
        if self._proxy:
            self._proxy.start()
        rc: int | None = None
        try:
            self._proc = subprocess.Popen(self.command, cwd=self.cwd, env=self._env())
            rc = self._wait()
        except FileNotFoundError:
            self.session.append(Event(Kind.NOTE, Source.CLI,
                                      {"error": f"command not found: {self.command[0]}"}))
            rc = 127
        except OSError as exc:  # not executable, bad interpreter, ...
            self.session.append(Event(Kind.NOTE, Source.CLI,
                                      {"error": f"cannot launch {self.command[0]}: {exc}"}))
            rc = 126
        finally:
            if self._proxy:
                self._proxy.stop()
            self._exec.stop()
            self._fs.stop()
            self._shim.cleanup()
            self.session.end(rc)
        return rc if rc is not None else 1

    def _wait(self) -> int:
        """Wait for the child, forwarding Ctrl-C so interactive harnesses exit cleanly."""
        assert self._proc is not None
        while True:
            try:
                return self._proc.wait()
            except KeyboardInterrupt:
                # The child is in our process group and already got SIGINT;
                # give it a moment, then escalate if it ignores us.
                time.sleep(0.5)
                if self._proc.poll() is None:
                    self._proc.send_signal(signal.SIGTERM)
