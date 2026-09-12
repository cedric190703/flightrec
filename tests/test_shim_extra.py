"""Shim generation and the exec log collector."""

import json
import os
import stat
import subprocess
from pathlib import Path

from flightrec import shimlog
from flightrec.events import Kind
from flightrec.shim import ExecCollector, ShimDir, _TEMPLATE
from flightrec.store import create_session


def test_shim_handles_real_path_with_quote_and_space(tmp_path: Path):
    bindir = tmp_path / "it's a bin"
    bindir.mkdir()
    tool = bindir / "mytool"
    tool.write_text("#!/bin/sh\necho ran:$1\nexit 7\n")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)

    session = create_session(tmp_path / "home")
    shim = ShimDir(session, extra_tools=["mytool"])
    shim.build(base_path=str(bindir))
    try:
        assert shim.shimmed["mytool"] == str(tool)
        r = subprocess.run(["mytool", "arg"], env=shim.env(), capture_output=True, text=True)
        assert r.returncode == 7 and "ran:arg" in r.stdout, r.stderr
        c = ExecCollector(session, shim.log_path)
        c.drain()
    finally:
        shim.cleanup()
    start, end = [e for e in session.events() if e.kind == Kind.EXEC]
    assert start.payload["command"] == "mytool arg" and end.payload["exit_code"] == 7
    assert not shim.dir.exists()


def test_build_skips_previous_shim_dirs_on_path(tmp_path: Path):
    stale = tmp_path / "flightrec-shim-old"
    stale.mkdir()
    fake = stale / "git"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    session = create_session(tmp_path / "home")
    shim = ShimDir(session)
    shim.build(base_path=str(stale) + os.pathsep + os.environ.get("PATH", ""))
    try:
        assert shim.shimmed["git"] != str(fake)
        assert "flightrec-shim-old" not in shim.shimmed["git"]
    finally:
        shim.cleanup()


def test_env_prepends_dir_and_clears_nesting_marker(tmp_path: Path):
    shim = ShimDir(create_session(tmp_path / "home"))
    env = shim.env({"PATH": "/usr/bin", "FLIGHTREC_IN_SHIM": "1"})
    assert env["PATH"].split(os.pathsep)[0] == str(shim.dir)
    assert "FLIGHTREC_IN_SHIM" not in env
    assert env["FLIGHTREC_EXEC_LOG"] == str(shim.log_path)
    shim.cleanup()


def test_template_is_posix_sh_and_passes_through_without_log():
    assert _TEMPLATE.startswith("#!/bin/sh\n")
    assert 'exec "$_real" "$@"' in _TEMPLATE


def test_collector_waits_for_partial_lines_and_skips_garbage(tmp_path: Path):
    session = create_session(tmp_path / "home")
    log = tmp_path / "exec.jsonl"
    c = ExecCollector(session, log)
    c.drain()                                    # log does not exist yet: no-op
    start = {"id": "s1", "phase": "start", "ts": 10.0, "real": "/bin/ls", "argv": ["-l"], "cwd": "/"}
    line = json.dumps(start)
    log.write_text(line[:20])                    # partially written
    c.drain()
    assert list(session.events()) == []
    log.write_text(line + "\n" + "garbage\n" + "[1,2]\n")
    c.drain()
    (ev,) = session.events()
    assert ev.payload["command"] == "ls -l" and ev.ts == 10.0
    # An end record with no matching start is ignored; a matching one links back.
    with log.open("a") as f:
        f.write(json.dumps({"id": "unknown", "phase": "end", "ts": 11.0, "exit_code": 1}) + "\n")
        f.write(json.dumps({"id": "s1", "phase": "end", "ts": 12.5, "exit_code": 0}) + "\n")
    c.drain()
    evs = list(session.events())
    assert len(evs) == 2
    assert evs[1].links == [ev.id] and evs[1].payload["duration"] == 2.5


def test_collector_start_record_with_missing_fields(tmp_path: Path):
    session = create_session(tmp_path / "home")
    c = ExecCollector(session, tmp_path / "exec.jsonl")
    c._handle({"id": "x", "phase": "start"})
    (ev,) = session.events()
    assert ev.payload["command"] == "" and ev.payload["argv"] == [] and isinstance(ev.ts, float)


def test_shimlog_main_writes_records(tmp_path: Path, monkeypatch, capsys):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("FLIGHTREC_EXEC_LOG", str(log))
    monkeypatch.chdir(tmp_path)
    assert shimlog.main(["start", "/bin/echo", "a", "b"]) == 0
    rec_id = capsys.readouterr().out
    assert len(rec_id) == 12
    assert shimlog.main(["end", rec_id, "5"]) == 0
    assert shimlog.main(["end", rec_id]) == 0            # no exit code -> None
    assert shimlog.main(["bogus", "x"]) == 0             # unknown mode is a no-op
    recs = [json.loads(l) for l in log.read_text().splitlines()]
    assert recs[0]["phase"] == "start" and recs[0]["argv"] == ["a", "b"]
    assert recs[0]["cwd"] == str(tmp_path) and recs[0]["real"] == "/bin/echo"
    assert recs[1] == {"id": rec_id, "phase": "end", "ts": recs[1]["ts"], "exit_code": 5}
    assert recs[2]["exit_code"] is None and len(recs) == 3


def test_shimlog_noop_without_env_or_args(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("FLIGHTREC_EXEC_LOG", raising=False)
    assert shimlog.main(["start", "/bin/echo"]) == 0
    monkeypatch.setenv("FLIGHTREC_EXEC_LOG", str(tmp_path / "l"))
    assert shimlog.main(["start"]) == 0                  # too few args
    assert not (tmp_path / "l").exists()
