from flightrec.events import Event, Kind, Snapshot, Source
from flightrec.timeline import build_steps, files_at


def _ev(kind, payload, ts, id=None, links=None, snaps=None, seq=None):
    e = Event(kind, Source.PROXY, payload, ts=ts, links=links or [], snapshots=snaps or [])
    if id:
        e.id = id
    e.seq = seq
    return e


def test_tool_call_absorbs_fs_and_exec_between_call_and_result():
    evs = [
        _ev(Kind.SESSION_START, {}, 0.0, seq=0),
        _ev(Kind.USER_MESSAGE, {"text": "fix it"}, 1.0, seq=1),
        _ev(Kind.LLM_RESPONSE, {"usage": {"input": 10, "output": 5}, "text": ""}, 2.0, seq=2),
        _ev(Kind.TOOL_CALL, {"name": "edit", "input": {"path": "src/a.py"}}, 2.0, id="c1", seq=3),
        _ev(Kind.FS_CHANGE, {"path": "src/a.py", "op": "modify", "size": 3}, 2.1, seq=4,
            snaps=[Snapshot("src/a.py", "h0", "h1")]),
        _ev(Kind.EXEC, {"command": "bash -c pytest", "argv": ["-c", "pytest"]}, 2.2, id="x1", seq=5),
        _ev(Kind.EXEC, {"phase": "end", "exit_code": 1, "duration": 0.3}, 2.5, id="x1e", links=["x1"], seq=6),
        _ev(Kind.TOOL_RESULT, {"content": "ok"}, 2.6, id="r1", links=["c1"], seq=7),
        _ev(Kind.LLM_RESPONSE, {"usage": {"input": 20, "output": 2}, "text": "Done"}, 3.0, seq=8),
        _ev(Kind.FS_CHANGE, {"path": "stray.txt", "op": "create", "size": 1}, 4.0, seq=9,
            snaps=[Snapshot("stray.txt", None, "h2")]),
        _ev(Kind.SESSION_END, {"exit_code": 0}, 5.0, seq=10),
    ]
    steps = build_steps(evs)
    kinds = [s.kind for s in steps]
    assert kinds == ["session", "user", "tool", "assistant", "fs", "session"]

    tool = steps[2]
    assert tool.title == "edit"
    assert tool.confidence == "strong"           # input mentions src/a.py
    assert [f["path"] for f in tool.fs] == ["src/a.py"]
    assert tool.execs[0]["exit_code"] == 1 and tool.execs[0]["duration"] == 0.3
    assert tool.result["content"] == "ok"
    assert tool.duration == 0.6
    assert tool.cumulative_tokens == 15
    assert tool.usage == {"input": 10, "output": 5}     # the response that produced the call
    assert steps[1].cumulative_tokens == 0              # user step: nothing spent yet

    assert steps[3].cumulative_tokens == 37
    assert steps[4].title == "create stray.txt"   # orphan fs event becomes its own step


def test_timing_only_attribution():
    evs = [
        _ev(Kind.TOOL_CALL, {"name": "run", "input": {"cmd": "make"}}, 1.0, id="c1", seq=0),
        _ev(Kind.FS_CHANGE, {"path": "build/out.o", "op": "create", "size": 1}, 1.1, seq=1),
        _ev(Kind.TOOL_RESULT, {"content": ""}, 1.5, id="r1", links=["c1"], seq=2),
    ]
    (step,) = build_steps(evs)
    assert step.confidence == "timing"


def test_command_substring_is_strong():
    evs = [
        _ev(Kind.TOOL_CALL, {"name": "Bash", "input": {"command": "pytest -q"}}, 1.0, id="c1", seq=0),
        _ev(Kind.EXEC, {"command": "bash -c pytest -q"}, 1.1, id="x", seq=1),
        _ev(Kind.TOOL_RESULT, {"content": ""}, 1.5, id="r1", links=["c1"], seq=2),
    ]
    (step,) = build_steps(evs)
    assert step.confidence == "strong"


def test_no_proxy_recording_still_yields_steps():
    evs = [
        _ev(Kind.EXEC, {"command": "git status"}, 1.0, id="x", seq=0),
        _ev(Kind.EXEC, {"phase": "end", "exit_code": 0, "duration": 0.1}, 1.1, links=["x"], seq=1),
        _ev(Kind.FS_CHANGE, {"path": "a", "op": "create", "size": 1}, 1.05, seq=2),
        _ev(Kind.FS_CHANGE, {"path": "b", "op": "create", "size": 1}, 5.0, seq=3),
    ]
    steps = build_steps(evs)
    assert [s.kind for s in steps] == ["exec", "fs"]
    assert steps[0].execs[0]["exit_code"] == 0
    assert [f["path"] for f in steps[0].fs] == ["a"]   # written while the command ran
    assert steps[1].title == "create b"                 # long after -> its own step


def test_files_at():
    evs = [
        _ev(Kind.FS_CHANGE, {"path": "a", "op": "create"}, 1, seq=0, snaps=[Snapshot("a", None, "h1")]),
        _ev(Kind.FS_CHANGE, {"path": "a", "op": "modify"}, 2, seq=1, snaps=[Snapshot("a", "h1", "h2")]),
        _ev(Kind.FS_CHANGE, {"path": "a", "op": "delete"}, 3, seq=2, snaps=[Snapshot("a", "h2", None)]),
    ]
    assert files_at(evs, 0) == {"a": "h1"}
    assert files_at(evs, 1) == {"a": "h2"}
    assert files_at(evs) == {"a": None}
