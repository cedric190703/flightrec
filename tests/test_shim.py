import os
import subprocess
import time
from pathlib import Path

from flightrec.events import Kind
from flightrec.shim import ExecCollector, ShimDir
from flightrec.store import create_session


def test_shim_records_top_level_command_only(tmp_path: Path):
    session = create_session(tmp_path / "home")
    shim = ShimDir(session)
    shim.build()
    assert "bash" in shim.shimmed and "git" in shim.shimmed
    env = shim.env()
    collector = ExecCollector(session, shim.log_path)
    try:
        # bash is shimmed; the nested `git --version` must be suppressed.
        r = subprocess.run(["bash", "-c", "git --version >/dev/null; exit 3"],
                           env=env, cwd=tmp_path, capture_output=True, text=True)
        assert r.returncode == 3
        collector.drain()
    finally:
        shim.cleanup()

    execs = [e for e in session.events() if e.kind == Kind.EXEC]
    assert len(execs) == 2, [e.payload for e in execs]
    start, end = execs
    assert start.payload["argv"] == ["-c", "git --version >/dev/null; exit 3"]
    assert start.payload["real"] == shim.shimmed["bash"]
    assert end.payload["exit_code"] == 3
    assert end.links == [start.id]


def test_shim_passthrough_without_log(tmp_path: Path):
    session = create_session(tmp_path / "home")
    shim = ShimDir(session)
    shim.build()
    env = shim.env()
    del env["FLIGHTREC_EXEC_LOG"]
    try:
        r = subprocess.run(["git", "--version"], env=env, capture_output=True, text=True)
        assert r.returncode == 0 and "git version" in r.stdout
        assert not shim.log_path.exists()
    finally:
        shim.cleanup()
