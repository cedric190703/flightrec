from pathlib import Path

from flightrec.cli import main
from flightrec.events import Kind
from flightrec.store import list_sessions


def test_run_records_fs_and_exec(tmp_path: Path, capsys):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    proj.mkdir()
    script = "echo hello > out.txt; git --version > /dev/null; sleep 0.3"
    rc = main(["--home", str(home), "run", "--cwd", str(proj), "--", "bash", "-c", script])
    assert rc == 0

    (s,) = list_sessions(home)
    kinds = [e.kind for e in s.events()]
    assert kinds[0] == Kind.SESSION_START and kinds[-1] == Kind.SESSION_END
    assert Kind.FS_CHANGE in kinds
    fs = next(e for e in s.events() if e.kind == Kind.FS_CHANGE)
    assert fs.payload["path"] == "out.txt"
    # bash was launched by us directly (absolute resolution via Popen -> PATH),
    # so the shim saw it; the nested git must not appear.
    execs = [e for e in s.events() if e.kind == Kind.EXEC and "phase" not in e.payload]
    assert len(execs) == 1 and execs[0].payload["command"].startswith("bash -c ")

    assert main(["--home", str(home), "list"]) == 0
    assert s.id in capsys.readouterr().out
    assert main(["--home", str(home), "show", s.id]) == 0
    assert "out.txt" in capsys.readouterr().out


def test_run_missing_command(tmp_path: Path):
    rc = main(["--home", str(tmp_path), "run", "--", "definitely-not-a-real-binary-xyz"])
    assert rc == 127
