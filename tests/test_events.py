"""Event serialisation: compactness, forward compatibility, bad input."""

import json

import pytest

from flightrec.events import Event, Kind, Snapshot, Source


def test_to_json_is_compact_single_line():
    e = Event(Kind.NOTE, Source.CLI, {"msg": "héllo"})
    line = e.to_json()
    assert "\n" not in line and ": " not in line and "héllo" in line   # ensure_ascii=False


def test_from_json_ignores_unknown_keys():
    line = json.dumps({"kind": "note", "source": "cli", "payload": {}, "ts": 1.0,
                       "id": "abc", "seq": 3, "links": [], "snapshots": [],
                       "future_field": {"added": "by a newer flightrec"}})
    e = Event.from_json(line)
    assert e.id == "abc" and e.seq == 3 and e.kind == "note"


def test_from_json_tolerates_missing_optional_keys():
    e = Event.from_json('{"kind":"note","source":"cli"}')
    assert e.payload == {} and e.links == [] and e.snapshots == [] and e.seq is None
    assert isinstance(e.ts, float) and len(e.id) == 12


def test_from_json_snapshot_with_extra_or_missing_keys():
    line = json.dumps({"kind": "fs_change", "source": "fs",
                       "snapshots": [{"path": "a", "before": None, "after": "h", "extra": 1},
                                     {"path": "b"}, "not-a-dict"]})
    e = Event.from_json(line)
    assert [s.path for s in e.snapshots] == ["a", "b"]
    assert e.snapshots[1].before is None and e.snapshots[1].after is None


def test_from_json_rejects_non_object():
    with pytest.raises(ValueError):
        Event.from_json("[1, 2, 3]")


def test_kind_and_source_are_plain_strings_on_the_wire():
    e = Event(Kind.EXEC, Source.SHIM)
    d = json.loads(e.to_json())
    assert d["kind"] == "exec" and d["source"] == "shim"
    assert Event.from_json(e.to_json()).kind == Kind.EXEC   # StrEnum compares equal to str
