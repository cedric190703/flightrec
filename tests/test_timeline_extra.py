"""Edge cases for the correlator."""

from flightrec.events import Event, Kind, Snapshot, Source
from flightrec.timeline import build_steps, files_at


def _ev(kind, payload, ts, id=None, links=None, snaps=None, seq=None):
    e = Event(kind, Source.PROXY, payload, ts=ts, links=links or [], snapshots=snaps or [])
    if id:
        e.id = id
    e.seq = seq
    return e


def test_empty_log_yields_no_steps():
    assert build_steps([]) == []


def test_orphan_tool_result_without_call_is_ignored():
    evs = [_ev(Kind.TOOL_RESULT, {"content": "stray"}, 1.0, id="r", links=["missing"], seq=0)]
    # A result whose call we never saw should not crash and produces no step.
    assert build_steps(evs) == []


def test_multiple_files_in_one_tool_call():
    evs = [
        _ev(Kind.TOOL_CALL, {"name": "ApplyPatch", "input": {"files": ["a.py", "b.py"]}}, 1.0, id="c", seq=0),
        _ev(Kind.FS_CHANGE, {"path": "a.py", "op": "modify", "size": 1}, 1.1, seq=1,
            snaps=[Snapshot("a.py", "h0", "h1")]),
        _ev(Kind.FS_CHANGE, {"path": "b.py", "op": "modify", "size": 1}, 1.2, seq=2,
            snaps=[Snapshot("b.py", "h0", "h1")]),
        _ev(Kind.TOOL_RESULT, {"content": "ok"}, 1.5, id="r", links=["c"], seq=3),
    ]
    (step,) = build_steps(evs)
    assert [f["path"] for f in step.fs] == ["a.py", "b.py"]
    assert step.confidence == "strong"       # both paths named in the input


def test_fs_change_after_result_starts_new_step():
    evs = [
        _ev(Kind.TOOL_CALL, {"name": "edit", "input": {"path": "a.py"}}, 1.0, id="c", seq=0),
        _ev(Kind.TOOL_RESULT, {"content": "ok"}, 1.2, id="r", links=["c"], seq=1),
        _ev(Kind.FS_CHANGE, {"path": "late.py", "op": "create", "size": 1}, 9.0, seq=2,
            snaps=[Snapshot("late.py", None, "h")]),
    ]
    steps = build_steps(evs)
    assert [s.kind for s in steps] == ["tool", "fs"]
    assert steps[0].fs == []                  # change long after result is not attributed


def test_assistant_text_without_usage_still_becomes_step():
    evs = [
        _ev(Kind.LLM_RESPONSE, {"text": "Here is my plan.", "usage": {}}, 1.0, seq=0),
    ]
    (step,) = build_steps(evs)
    assert step.kind == "assistant" and step.title == "Here is my plan."


def test_error_tool_result_is_surfaced():
    evs = [
        _ev(Kind.TOOL_CALL, {"name": "Bash", "input": {"command": "false"}}, 1.0, id="c", seq=0),
        _ev(Kind.TOOL_RESULT, {"content": "command failed", "is_error": True}, 1.2,
            id="r", links=["c"], seq=1),
    ]
    (step,) = build_steps(evs)
    assert step.result["is_error"] is True


def test_files_at_before_anything_is_empty():
    evs = [_ev(Kind.FS_CHANGE, {"path": "a"}, 1.0, seq=5, snaps=[Snapshot("a", None, "h")])]
    assert files_at(evs, upto_seq=0) == {}     # event at seq 5 is in the future
    assert files_at(evs, upto_seq=5) == {"a": "h"}
