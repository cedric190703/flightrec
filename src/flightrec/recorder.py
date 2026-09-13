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
        fs_started = exec_started = proxy_started = False
        rc = 1
        try:
            self._shim.build()
            self._fs.start()
            fs_started = True
            self._exec.start()
            exec_started = True
            if self._proxy:
                self._proxy.start()
                proxy_started = True
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
        except Exception as exc:  # setup must not leave an unclosed session behind
            self.session.append(Event(Kind.NOTE, Source.CLI,
                                      {"error": f"recorder setup failed: {exc}"}))
            rc = 125
        finally:
            if proxy_started and self._proxy:
                self._proxy.stop()
            if exec_started:
                self._exec.stop()
            if fs_started:
                self._fs.stop()
            self._shim.cleanup()
            self.session.end(rc)
        return rc

    # Escalation on repeated Ctrl-C: the child already received SIGINT with
    # us; if it is still alive after a grace period we send these in turn.
    _ESCALATION = (signal.SIGTERM, signal.SIGKILL)
    _GRACE_S = 0.5

    def _wait(self) -> int:
        """Wait for the child, forwarding Ctrl-C so interactive harnesses exit cleanly."""
        assert self._proc is not None
        interrupts = 0
        while True:
            try:
                return self._proc.wait()
            except KeyboardInterrupt:
                time.sleep(self._GRACE_S)
                if self._proc.poll() is not None:
                    continue
                sig = self._ESCALATION[min(interrupts, len(self._ESCALATION) - 1)]
                interrupts += 1
                try:
                    self._proc.send_signal(sig)
                except ProcessLookupError:
                    pass  # exited between poll() and the signal
