"""Recorder.__wait: Ctrl-C forwarding escalates from SIGTERM to SIGKILL and
never raises when the child exits on its own during the grace period."""

import signal
from pathlib import Path

from flightrec.recorder import Recorder


class _FakeProc:
    """A child that raises KeyboardInterrupt from wait() ``interrupts`` times,
    ignores every signal until ``dies_on`` is sent, then exits with ``rc``."""

    def __init__(self, interrupts: int, dies_on: int | None, rc: int = 0):
        self.interrupts = interrupts
        self.dies_on = dies_on
        self.rc = rc
        self.sent: list[int] = []
        self._alive = True

    def wait(self):
        if self.interrupts > 0:
            self.interrupts -= 1
            raise KeyboardInterrupt
        self._alive = False
        return self.rc

    def poll(self):
        return None if self._alive else self.rc

    def send_signal(self, sig):
        self.sent.append(sig)
        if sig == self.dies_on:
            self._alive = False
            self.rc = -sig


def _recorder(tmp_path: Path, proc) -> Recorder:
    rec = Recorder(["true"], tmp_path, root=tmp_path / "home", proxy=False)
    rec._proc = proc
    rec._GRACE_S = 0.0
    return rec


def test_first_interrupt_sends_sigterm(tmp_path: Path):
    proc = _FakeProc(interrupts=1, dies_on=signal.SIGTERM)
    assert _recorder(tmp_path, proc)._wait() == -signal.SIGTERM
    assert proc.sent == [signal.SIGTERM]


def test_repeated_interrupts_escalate_to_sigkill(tmp_path: Path):
    proc = _FakeProc(interrupts=3, dies_on=signal.SIGKILL)
    assert _recorder(tmp_path, proc)._wait() == -signal.SIGKILL
    assert proc.sent == [signal.SIGTERM, signal.SIGKILL]   # third Ctrl-C finds it dead


def test_child_exiting_during_grace_gets_no_signal(tmp_path: Path):
    class Exiting(_FakeProc):
        def poll(self):
            self._alive = False   # it died right after SIGINT
            return 130
    proc = Exiting(interrupts=1, dies_on=None, rc=130)
    assert _recorder(tmp_path, proc)._wait() == 130
    assert proc.sent == []


def test_process_vanishing_before_signal_is_not_an_error(tmp_path: Path):
    class Vanishing(_FakeProc):
        def send_signal(self, sig):
            self.sent.append(sig)
            self._alive = False
            raise ProcessLookupError
    proc = Vanishing(interrupts=1, dies_on=None, rc=7)
    assert _recorder(tmp_path, proc)._wait() == 7
    assert proc.sent == [signal.SIGTERM]
