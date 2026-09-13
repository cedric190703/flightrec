"""CLI and recorder edge cases."""

import json
import stat
from pathlib import Path

import pytest

from flightrec.cli import _one_line, main
from flightrec.events import Kind
from flightrec.recorder import Recorder
from flightrec.store import list_sessions


def _run(home, proj, *cmd, extra=()):
    rc = main(["--home", str(home), "run", "--cwd", str(proj), "--no-proxy", *extra, "--", *cmd])
    (s,) = list_sessions(home)
    return rc, s


def test_one_line_collapses_whitespace():
    assert _one_line("a\n  b\t\tc\n", 10) == "a b c"
    assert _one_line("x" * 20, 5) == "xxxxx"


def test_list_with_no_sessions(tmp_path: Path, capsys):
    assert main(["--home", str(tmp_path), "list"]) == 0
    assert "no sessions" in capsys.readouterr().out


def test_show_and_fork_missing_session(tmp_path: Path, capsys):
    assert main(["--home", str(tmp_path), "show", "nope"]) == 1
    assert "nope" in capsys.readouterr().err
    assert main(["--home", str(tmp_path), "fork", "nope", "--at", "1", "--dest", str(tmp_path / "d")]) == 1


def test_run_requires_command_and_existing_cwd(tmp_path: Path, capsys):
    assert main(["--home", str(tmp_path), "run"]) == 2
    assert main(["--home", str(tmp_path), "run", "--"]) == 2
    assert main(["--home", str(tmp_path), "run", "--cwd", str(tmp_path / "missing"), "--", "true"]) == 2
    assert "not a directory" in capsys.readouterr().err
    assert list((tmp_path / "sessions").iterdir()) == [] if (tmp_path / "sessions").exists() else True


def test_version_flag():
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0


def test_run_non_executable_command_records_note(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    script = proj / "noexec.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(stat.S_IRUSR | stat.S_IWUSR)        # no x bit
    rc, s = _run(tmp_path / "home", proj, str(script))
    assert rc == 126
    notes = [e for e in s.events() if e.kind == Kind.NOTE]
    assert notes and "cannot launch" in notes[0].payload["error"]
    assert s.read_meta()["exit_code"] == 126
    assert [e.kind for e in s.events()][-1] == Kind.SESSION_END


def test_run_propagates_child_exit_code_and_harness_label(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    rc, s = _run(tmp_path / "home", proj, "bash", "-c", "exit 9", extra=["--harness", "mybot"])
    assert rc == 9
    meta = s.read_meta()
    assert meta["harness"] == "mybot" and meta["exit_code"] == 9 and meta["cwd"] == str(proj.resolve())


def test_run_exports_session_id_and_honours_ignore(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    rc, s = _run(tmp_path / "home", proj, "bash", "-c",
                 "echo $FLIGHTREC_SESSION > sid.txt; echo x > noise.log; sleep 0.3",
                 extra=["--ignore", "*.log"])
    assert rc == 0
    assert (proj / "sid.txt").read_text().strip() == s.id
    paths = {e.payload["path"] for e in s.events() if e.kind == Kind.FS_CHANGE}
    assert "sid.txt" in paths and "noise.log" not in paths


def test_show_kind_filter(tmp_path: Path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    _, s = _run(tmp_path / "home", proj, "bash", "-c", "echo a > a.txt; sleep 0.3")
    capsys.readouterr()
    assert main(["--home", str(tmp_path / "home"), "show", "--kind", "fs_change", s.id]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out and all("fs_change" in line for line in out)
    assert main(["--home", str(tmp_path / "home"), "show", "--kind", "session_end", s.id]) == 0
    assert "end (exit 0)" in capsys.readouterr().out


def test_show_steps_keeps_multiline_titles_on_one_row(tmp_path: Path, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    _, s = _run(tmp_path / "home", proj, "bash", "-c", "true")
    from flightrec.events import Event, Source
    s.append(Event(Kind.USER_MESSAGE, Source.PROXY, {"text": "first line\nsecond line"}))
    capsys.readouterr()
    assert main(["--home", str(tmp_path / "home"), "show", "--steps", s.id]) == 0
    out = capsys.readouterr().out
    assert "USER  first line second line" in out


def test_recorder_env_composition(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    r = Recorder(["true"], proj, root=tmp_path / "home", proxy=True)
    env = r._env()
    assert env["FLIGHTREC_SESSION"] == r.session.id
    assert env["PATH"].startswith(str(r._shim.dir))
    assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    r._shim.cleanup()
    r._proxy.stop()
    no_proxy = Recorder(["true"], proj, root=tmp_path / "home", proxy=False)
    assert "FLIGHTREC_PROXY" not in no_proxy._env()
    no_proxy._shim.cleanup()


def test_recorder_setup_failure_closes_session_and_returns_125(tmp_path: Path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    r = Recorder(["true"], proj, root=tmp_path / "home", proxy=False)

    def fail_start():
        raise RuntimeError("watcher unavailable")

    monkeypatch.setattr(r._fs, "start", fail_start)
    assert r.run() == 125
    assert r.session.read_meta()["exit_code"] == 125
    events = list(r.session.events())
    assert events[-1].kind == Kind.SESSION_END
    note = next(e for e in events if e.kind == Kind.NOTE)
    assert "recorder setup failed: watcher unavailable" == note.payload["error"]
