"""Correlator: grace window, open exec steps, mixed text+tool responses,
usage accounting, ordering ties, and the mention heuristic."""

import json

from flightrec.events import Event, Kind, Snapshot, Source
from flightrec.timeline import ATTRIBUTION_GRACE_S, Step, _mentions, _strings, build_steps


def _ev(kind, payload, ts, id=None, links=None, snaps=None, seq=None):
    e = Event(kind, Source.PROXY, payload, ts=ts, links=links or [], snapshots=snaps or [])
    if id:
        e.id = id
    e.seq = seq
    return e


def _call_result(fs_ts):
    return [
        _ev(Kind.TOOL_CALL, {"name": "Write", "input": {"file_path": "/abs/proj/a.py"}}, 1.0, id="c", seq=0),
        _ev(Kind.TOOL_RESULT, {"content": "ok"}, 1.2, id="r", links=["c"], seq=1),
        _ev(Kind.FS_CHANGE, {"path": "a.py", "op": "modify", "size": 1}, fs_ts, seq=2,
            snaps=[Snapshot("a.py", "h0", "h1")]),
    ]


def test_fs_change_inside_grace_window_is_attributed():
    steps = build_steps(_call_result(1.2 + ATTRIBUTION_GRACE_S - 0.01))
    (step,) = steps
    assert step.kind == "tool" and [f["path"] for f in step.fs] == ["a.py"]
    assert step.confidence == "strong"       # rel path matches the tail of the absolute input


def test_fs_change_just_outside_grace_window_is_separate():
    steps = build_steps(_call_result(1.2 + ATTRIBUTION_GRACE_S + 0.01))
    assert [s.kind for s in steps] == ["tool", "fs"]
    assert steps[0].confidence == "n/a"      # nothing attributed -> no confidence claim


def test_exec_without_end_event_stays_open_until_next_step():
    evs = [
        _ev(Kind.EXEC, {"command": "bash -c make"}, 1.0, id="x", seq=0),
        _ev(Kind.FS_CHANGE, {"path": "out.o", "op": "create", "size": 1}, 30.0, seq=1),
        _ev(Kind.USER_MESSAGE, {"text": "next"}, 40.0, seq=2),
        _ev(Kind.FS_CHANGE, {"path": "late", "op": "create", "size": 1}, 41.0, seq=3),
    ]
    steps = build_steps(evs)
    assert [s.kind for s in steps] == ["exec", "user", "fs"]
    assert [f["path"] for f in steps[0].fs] == ["out.o"]
    assert steps[0].execs[0]["exit_code"] is None and steps[0].duration is None


def test_response_with_text_and_tool_call_yields_two_steps():
    evs = [
        _ev(Kind.LLM_RESPONSE, {"text": "Editing now.", "usage": {"input": 5, "output": 1},
                                "latency": 0.4}, 2.0, seq=0),
        _ev(Kind.TOOL_CALL, {"name": "Edit", "input": {}}, 2.0, id="c", seq=1),
        _ev(Kind.TOOL_RESULT, {"content": "ok"}, 2.5, id="r", links=["c"], seq=2),
    ]
    a, t = build_steps(evs)
    assert a.kind == "assistant" and a.usage == {"input": 5, "output": 1} and a.duration == 0.4
    assert a.cumulative_tokens == 6
    assert t.kind == "tool" and t.usage is None and t.cumulative_tokens == 6


def test_pending_usage_goes_to_first_call_only():
    evs = [
        _ev(Kind.LLM_RESPONSE, {"text": "", "usage": {"input": 3, "output": 4}}, 1.0, seq=0),
        _ev(Kind.TOOL_CALL, {"name": "A", "input": {}}, 1.0, id="c1", seq=1),
        _ev(Kind.TOOL_CALL, {"name": "B", "input": {}}, 1.0, id="c2", seq=2),
    ]
    a, b = build_steps(evs)
    assert a.usage == {"input": 3, "output": 4} and b.usage is None
    assert a.cumulative_tokens == 7 and b.cumulative_tokens == 7


def test_usage_with_missing_fields_counts_zero():
    evs = [_ev(Kind.LLM_RESPONSE, {"text": "t", "usage": {"input": None}}, 1.0, seq=0),
           _ev(Kind.LLM_RESPONSE, {"text": "u"}, 2.0, seq=1)]
    s1, s2 = build_steps(evs)
    assert s1.cumulative_tokens == 0 and s2.cumulative_tokens == 0


def test_equal_timestamps_are_ordered_by_seq():
    evs = [
        _ev(Kind.TOOL_CALL, {"name": "B", "input": {}}, 1.0, id="c2", seq=5),
        _ev(Kind.TOOL_CALL, {"name": "A", "input": {}}, 1.0, id="c1", seq=4),
        _ev(Kind.SESSION_START, {}, 1.0, seq=0),
    ]
    assert [s.title for s in build_steps(evs)] == ["session start", "A", "B"]
    assert [s.index for s in build_steps(evs)] == [0, 1, 2]


def test_session_end_title_and_llm_request_not_a_step():
    evs = [_ev(Kind.LLM_REQUEST, {"model": "m"}, 0.5, seq=0),
           _ev(Kind.NOTE, {"error": "x"}, 0.6, seq=1),
           _ev(Kind.SESSION_END, {"exit_code": 2}, 1.0, seq=2)]
    (s,) = build_steps(evs)
    assert s.title == "session end (exit 2)"


def test_user_title_is_truncated_but_text_is_full():
    text = "x" * 200
    (s,) = build_steps([_ev(Kind.USER_MESSAGE, {"text": text}, 1.0, seq=0)])
    assert len(s.title) == 80 and s.text == text


def test_fs_event_without_snapshot_still_attributed():
    evs = [_ev(Kind.TOOL_CALL, {"name": "Edit", "input": {"path": "f.py"}}, 1.0, id="c", seq=0),
           _ev(Kind.FS_CHANGE, {"path": "f.py", "op": "modify"}, 1.1, seq=1)]
    (s,) = build_steps(evs)
    assert s.fs[0]["before"] is None and s.fs[0]["after"] is None and s.fs[0]["size"] is None


def test_step_to_dict_is_json_serialisable():
    evs = [_ev(Kind.TOOL_CALL, {"name": "E", "input": {"p": "x.py"}}, 1.0, id="c", seq=0)]
    d = build_steps(evs)[0].to_dict()
    assert json.loads(json.dumps(d))["call"]["name"] == "E"
    assert set(d) == {f for f in Step.__dataclass_fields__}


def test_mentions_heuristic():
    assert _mentions({"path": "src/app.py"}, "src/app.py")
    assert _mentions({"path": "/home/u/proj/src/app.py"}, "src/app.py")     # tail match
    assert _mentions({"command": "pytest -q tests"}, "bash -c pytest -q tests")
    assert not _mentions({"command": "ls"}, "bash -c cat x")                 # short value never matches
    assert not _mentions({"path": "a.py"}, "b.py")
    assert not _mentions(None, "a.py") and not _mentions({"p": "a"}, "")
    assert not _mentions({"path": "a.py"}, "b/c")        # 1-char tail does not match by accident


def test_strings_flattens_nested_input():
    assert _strings({"a": ["x", {"b": "y"}], "c": 1, "d": None}) == ["x", "y"]
