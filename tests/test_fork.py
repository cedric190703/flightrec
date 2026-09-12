from pathlib import Path

from flightrec.events import Event, Kind, Snapshot, Source
from flightrec.fork import fork, materialize
from flightrec.store import create_session


def _build(tmp_path):
    s = create_session(tmp_path / "home")
    b0 = s.blobs.put(b"return a - b\n")
    b1 = s.blobs.put(b"return a + b\n")
    bt = s.blobs.put(b"def test(): pass\n")

    def ev(kind, payload, seq, **kw):
        e = Event(kind, kw.pop("source", Source.PROXY), payload, ts=float(seq), **kw)
        if "id" in kw:
            e.id = kw["id"]
        s.append(e)
        return e

    ev(Kind.USER_MESSAGE, {"text": "fix add"}, 0)
    c = Event(Kind.TOOL_CALL, Source.PROXY, {"name": "Edit", "input": {"file_path": "calc.py"}}, ts=1.0)
    c.id = "c1"; s.append(c)
    ev(Kind.FS_CHANGE, {"path": "calc.py", "op": "modify", "size": 12}, 2, source=Source.FS,
       snapshots=[Snapshot("calc.py", b0, b1)])
    ev(Kind.TOOL_RESULT, {"name": "Edit", "is_error": False, "content": "ok"}, 3, links=["c1"])
    ev(Kind.FS_CHANGE, {"path": "test_calc.py", "op": "create", "size": 16}, 4, source=Source.FS,
       snapshots=[Snapshot("test_calc.py", None, bt)])
    return s


def test_materialize_restores_state_at_step(tmp_path: Path):
    s = _build(tmp_path)
    # Before the edit's fs_change (seq 2): calc.py should be the ORIGINAL content.
    early = tmp_path / "early"
    materialize(s, upto_seq=1, dest=early)
    assert (early / "calc.py").read_text() == "return a - b\n"
    assert not (early / "test_calc.py").exists()

    # After the edit but before the new test file (seq 2).
    mid = tmp_path / "mid"
    materialize(s, upto_seq=2, dest=mid)
    assert (mid / "calc.py").read_text() == "return a + b\n"
    assert not (mid / "test_calc.py").exists()

    # At the end: both files present.
    end = tmp_path / "end"
    materialize(s, upto_seq=99, dest=end)
    assert (end / "calc.py").read_text() == "return a + b\n"
    assert (end / "test_calc.py").read_text() == "def test(): pass\n"


def test_fork_writes_summary(tmp_path: Path):
    s = _build(tmp_path)
    info = fork(s, upto_seq=3, dest=tmp_path / "forked")
    md = (tmp_path / "forked" / "FORK.md").read_text()
    assert "fix add" in md
    assert "**Edit**" in md and "calc.py" in md
    assert "calc.py" in info["files"]
