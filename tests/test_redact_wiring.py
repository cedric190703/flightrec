"""Redaction wiring: fswatch skips secret files, CLI threads --redact through."""

import time
from pathlib import Path

from flightrec.cli import main
from flightrec.events import Kind
from flightrec.fswatch import DEFAULT_IGNORES, FsWatcher, _is_ignored
from flightrec.recorder import Recorder
from flightrec.store import create_session


def _fs_events(session):
    return [e for e in session.events() if e.kind == Kind.FS_CHANGE]


def test_secret_files_are_ignored_by_default():
    for name in [".env", ".env.local", "server.pem", "tls.key", "id_rsa",
                 "id_ed25519", ".npmrc", ".netrc"]:
        assert _is_ignored(name, DEFAULT_IGNORES), name
    # ordinary source files still watched
    assert not _is_ignored("src/env_utils.py", DEFAULT_IGNORES)


def test_watcher_does_not_snapshot_a_dotenv(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    session = create_session(tmp_path / "home")
    w = FsWatcher(proj, session)
    w.start()
    try:
        time.sleep(0.3)
        (proj / ".env").write_text("OPENAI_API_KEY=sk-ant-secret000000000000000000")
        (proj / "app.py").write_text("print('hi')")
        deadline = time.time() + 6
        while time.time() < deadline and not any(
                e.payload.get("path") == "app.py" for e in _fs_events(session)):
            time.sleep(0.05)
    finally:
        w.stop()

    paths = {e.payload.get("path") for e in _fs_events(session)}
    assert "app.py" in paths
    assert ".env" not in paths
    # And the secret never reached a blob.
    on_disk = "".join(f.read_text(errors="replace")
                      for f in session.dir.rglob("*") if f.is_file())
    assert "sk-ant-secret" not in on_disk


def test_recorder_reports_redaction_state(tmp_path: Path, monkeypatch):
    r = Recorder(["true"], tmp_path, root=tmp_path / "home", redact=["hunter2"])
    assert r.redaction_enabled is True
    monkeypatch.setenv("FLIGHTREC_NO_REDACT", "1")
    r2 = Recorder(["true"], tmp_path, root=tmp_path / "home")
    assert r2.redaction_enabled is False


def test_cli_run_accepts_redact_flag(tmp_path: Path):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    proj.mkdir()
    rc = main(["--home", str(home), "run", "--cwd", str(proj),
               "--redact", "myproj-secret", "--no-proxy", "--", "true"])
    assert rc == 0
