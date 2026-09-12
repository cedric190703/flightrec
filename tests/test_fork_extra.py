"""Fork: deletions, files created-then-deleted, summaries, overwriting."""

from pathlib import Path

from flightrec.cli import main
from flightrec.events import Event, Kind, Snapshot, Source
from flightrec.fork import _original_tree, fork, materialize, summary_markdown
from flightrec.store import create_session


def _session(tmp_path):
    s = create_session(tmp_path / "home")
    s.start(["agent", "--go"], cwd="/p", harness="agent")
    h_keep0 = s.blobs.put(b"keep v0\n")
    h_keep1 = s.blobs.put(b"keep v1\n")
    h_tmp = s.blobs.put(b"temporary\n")
    h_gone = s.blobs.put(b"was here\n")

    def fs(path, before, after, ts):
        s.append(Event(Kind.FS_CHANGE, Source.FS, {"path": path, "op": "x", "size": 0}, ts=ts,
                       snapshots=[Snapshot(path, before, after)]))

    fs("gone.txt", h_gone, None, 1.0)         # seq 1: deleted right away
    fs("tmp.txt", None, h_tmp, 2.0)           # seq 2: created...
    fs("keep.txt", h_keep0, h_keep1, 3.0)     # seq 3
    fs("tmp.txt", h_tmp, None, 4.0)           # seq 4: ...and deleted again
    x = Event(Kind.EXEC, Source.SHIM, {"command": "bash -c pytest"}, ts=5.0)
    s.append(x)                               # seq 5
    s.append(Event(Kind.EXEC, Source.SHIM, {"phase": "end", "exit_code": 1, "duration": 0.2},
                   ts=5.2, links=[x.id]))     # seq 6
    return s


def test_original_tree_uses_first_before(tmp_path):
    s = _session(tmp_path)
    o = _original_tree(s)
    assert set(o) == {"gone.txt", "keep.txt"}      # tmp.txt never existed originally
    assert s.blobs.get(o["keep.txt"]) == b"keep v0\n"


def test_materialize_respects_deletes_and_recreates(tmp_path):
    s = _session(tmp_path)
    d0 = materialize(s, 0, tmp_path / "d0")        # before anything: originals only
    assert sorted(d0) == ["gone.txt", "keep.txt"]
    assert (tmp_path / "d0" / "gone.txt").read_text() == "was here\n"
    d2 = materialize(s, 2, tmp_path / "d2")
    assert sorted(d2) == ["keep.txt", "tmp.txt"] and (tmp_path / "d2" / "keep.txt").read_text() == "keep v0\n"
    d4 = materialize(s, 4, tmp_path / "d4")
    assert d4 == ["keep.txt"] and (tmp_path / "d4" / "keep.txt").read_text() == "keep v1\n"


def test_materialize_skips_missing_blob(tmp_path):
    s = create_session(tmp_path / "home")
    s.append(Event(Kind.FS_CHANGE, Source.FS, {"path": "x", "op": "create", "size": 1},
                   snapshots=[Snapshot("x", None, "0" * 64)]))
    assert materialize(s, 99, tmp_path / "d") == []


def test_summary_lists_steps_up_to_fork_point(tmp_path):
    s = _session(tmp_path)
    md = summary_markdown(s, upto_seq=3)
    assert "Harness: **agent**" in md and "`agent --go`" in md and "Forked at step 3" in md
    assert "pytest" not in md                      # exec at seq 5 is after the fork
    md = summary_markdown(s, upto_seq=6)
    assert "**$ bash -c pytest** (exit 1)" in md


def test_summary_marks_errored_tool_steps(tmp_path):
    s = create_session(tmp_path / "home")
    c = Event(Kind.TOOL_CALL, Source.PROXY, {"name": "Bash", "input": {"command": "false"}}, ts=1.0)
    s.append(c)
    s.append(Event(Kind.TOOL_RESULT, Source.PROXY, {"is_error": True, "content": "boom"}, ts=1.5, links=[c.id]))
    md = summary_markdown(s, upto_seq=1)
    assert "- **Bash** → — _(error)_" in md


def test_fork_force_overwrites_existing_dir(tmp_path):
    s = _session(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "stale.txt").write_text("old")
    home = tmp_path / "home"
    assert main(["--home", str(home), "fork", s.id, "--at", "4", "--dest", str(dest)]) == 1
    assert main(["--home", str(home), "fork", s.id, "--at", "4", "--dest", str(dest), "--force"]) == 0
    assert (dest / "keep.txt").read_text() == "keep v1\n" and (dest / "FORK.md").exists()
    assert (dest / "stale.txt").exists()            # --force adds/overwrites, it does not wipe


def test_fork_returns_info(tmp_path):
    s = _session(tmp_path)
    info = fork(s, 3, tmp_path / "f")
    assert info["step"] == 3 and info["files"] == ["keep.txt", "tmp.txt"] and info["dest"].endswith("/f")
