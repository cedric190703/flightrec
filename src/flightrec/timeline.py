"""Correlator: turns the flat event log into a list of *steps*.

The three observers are independent: the proxy sees a ``tool_call`` for
``edit_file(path=a.py)``; a few ms later the fs watcher sees ``a.py``
change; the shim may see ``bash -c pytest``. Nothing on the wire ties them
together, so we do it here:

1. order events by wall-clock time (the proxy back-dates its events to
   when the request/response actually happened);
2. open a step at each ``tool_call``; every ``fs_change``/``exec`` that
   happens before the matching ``tool_result`` (or the next call) is
   attributed to it;
3. score the attribution: *strong* when the call's input mentions the
   path/command, *timing* otherwise;
4. events outside any call become their own steps, so recordings made
   without the proxy (``--no-proxy``, or a harness we could not intercept)
   still produce a usable timeline.

The result is computed on demand and never stored, so the raw log stays
the single source of truth.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Iterable

from .events import Event, Kind

# An fs/exec event this long after a tool_result still belongs to that call:
# observers stamp events when they *see* a change, and delivery lags slightly
# behind the harness reporting the tool as done.
ATTRIBUTION_GRACE_S = 0.25


@dataclass
class Step:
    index: int
    kind: str                     # user | assistant | tool | exec | fs | session
    ts: float
    title: str
    event_ids: list[str] = field(default_factory=list)
    call: dict | None = None
    result: dict | None = None
    fs: list[dict] = field(default_factory=list)
    execs: list[dict] = field(default_factory=list)
    text: str | None = None
    usage: dict | None = None
    cumulative_tokens: int = 0
    confidence: str = "n/a"       # strong | timing | n/a
    duration: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _strings(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [x for val in v.values() for x in _strings(val)]
    if isinstance(v, list):
        return [x for val in v for x in _strings(val)]
    return []


def _mentions(call_input: dict | None, needle: str) -> bool:
    """Does the tool call's input plausibly refer to this path or command?"""
    if not call_input or not needle:
        return False
    blob = json.dumps(call_input, ensure_ascii=False)
    tail = needle.rsplit("/", 1)[-1]
    if needle in blob or (len(tail) > 3 and tail in blob):
        return True
    # e.g. call input {"command": "pytest -q"} vs observed "bash -c pytest -q"
    return any(len(v) > 3 and v.strip() in needle for v in _strings(call_input))


def _fs_dict(e: Event) -> dict:
    snap = e.snapshots[0] if e.snapshots else None
    return {"id": e.id, "ts": e.ts, "path": e.payload.get("path") or "?",
            "op": e.payload.get("op") or "modify",
            "size": e.payload.get("size"),
            "before": snap.before if snap else None, "after": snap.after if snap else None}


def _exec_pairs(events: list[Event]) -> tuple[dict[str, dict], set[str]]:
    """Merge exec start/end pairs into one dict per command."""
    execs: dict[str, dict] = {}
    consumed: set[str] = set()
    for e in events:
        if e.kind != Kind.EXEC:
            continue
        if e.payload.get("phase") == "end":
            for start_id in e.links:
                if start_id in execs:
                    execs[start_id]["exit_code"] = e.payload.get("exit_code")
                    execs[start_id]["duration"] = e.payload.get("duration")
            consumed.add(e.id)
        else:
            execs[e.id] = {"id": e.id, "ts": e.ts, "command": e.payload.get("command"),
                           "argv": e.payload.get("argv"), "cwd": e.payload.get("cwd"),
                           "exit_code": None, "duration": None}
    return execs, consumed


def build_steps(events: Iterable[Event]) -> list[Step]:
    evs = sorted(events, key=lambda e: (e.ts, e.seq if e.seq is not None else 0))
    execs, consumed = _exec_pairs(evs)
    results_by_call: dict[str, Event] = {}
    for e in evs:
        if e.kind == Kind.TOOL_RESULT:
            for cid in e.links:
                results_by_call[cid] = e

    steps: list[Step] = []
    open_step: Step | None = None
    open_call_ts_end: float | None = None
    total_tokens = 0
    pending_usage: dict | None = None   # usage of a response whose tool_call has not been seen yet

    def close_open():
        nonlocal open_step, open_call_ts_end
        if open_step is not None:
            if open_step.kind == "tool" and (open_step.fs or open_step.execs):
                if open_step.confidence != "strong":
                    open_step.confidence = "timing"
            steps.append(open_step)
        open_step = None
        open_call_ts_end = None

    for e in evs:
        if e.id in consumed:
            continue
        k = e.kind
        if k == Kind.TOOL_CALL:
            close_open()
            res = results_by_call.get(e.id)
            open_step = Step(index=len(steps), kind="tool", ts=e.ts,
                             title=f"{e.payload.get('name')}",
                             event_ids=[e.id], call={"id": e.id, **e.payload})
            if res is not None:
                open_step.result = {"id": res.id, **res.payload}
                open_step.event_ids.append(res.id)
                open_step.duration = round(res.ts - e.ts, 3)
                open_call_ts_end = res.ts
            open_step.cumulative_tokens = total_tokens
            open_step.usage, pending_usage = pending_usage, None
        elif k == Kind.TOOL_RESULT:
            continue  # folded into its call above
        elif k == Kind.FS_CHANGE:
            d = _fs_dict(e)
            if open_step is not None and (open_call_ts_end is None or e.ts <= open_call_ts_end + ATTRIBUTION_GRACE_S):
                open_step.fs.append(d)
                open_step.event_ids.append(e.id)
                if _mentions(open_step.call.get("input") if open_step.call else None, d["path"]):
                    open_step.confidence = "strong"
            else:
                close_open()
                steps.append(Step(index=len(steps), kind="fs", ts=e.ts,
                                  title=f"{d['op']} {d['path']}", event_ids=[e.id], fs=[d],
                                  cumulative_tokens=total_tokens))
        elif k == Kind.EXEC:
            d = execs.get(e.id, {"id": e.id, "ts": e.ts, "command": e.payload.get("command")})
            if open_step is not None and (open_call_ts_end is None or e.ts <= open_call_ts_end + ATTRIBUTION_GRACE_S):
                open_step.execs.append(d)
                open_step.event_ids.append(e.id)
                if _mentions(open_step.call.get("input") if open_step.call else None,
                             d.get("command") or ""):
                    open_step.confidence = "strong"
            else:
                # A top-level command becomes an open step so file changes made
                # while it runs are attributed to it (no-proxy recordings).
                close_open()
                open_step = Step(index=len(steps), kind="exec", ts=e.ts,
                                 title=f"$ {d.get('command') or ''}", event_ids=[e.id], execs=[d],
                                 cumulative_tokens=total_tokens, duration=d.get("duration"))
                open_step.confidence = "timing"
                if d.get("duration") is not None:
                    open_call_ts_end = e.ts + d["duration"]
        elif k == Kind.USER_MESSAGE:
            close_open()
            steps.append(Step(index=len(steps), kind="user", ts=e.ts,
                              title=(e.payload.get("text") or "")[:80], event_ids=[e.id],
                              text=e.payload.get("text"), cumulative_tokens=total_tokens))
        elif k == Kind.LLM_RESPONSE:
            u = e.payload.get("usage") or {}
            total_tokens += (u.get("input") or 0) + (u.get("output") or 0)
            if e.payload.get("text"):
                close_open()
                steps.append(Step(index=len(steps), kind="assistant", ts=e.ts,
                                  title=e.payload["text"][:80], event_ids=[e.id],
                                  text=e.payload.get("text"), usage=u,
                                  cumulative_tokens=total_tokens,
                                  duration=e.payload.get("latency")))
            else:
                pending_usage = u
        elif k in (Kind.SESSION_START, Kind.SESSION_END):
            close_open()
            steps.append(Step(index=len(steps), kind="session", ts=e.ts,
                              title="session start" if k == Kind.SESSION_START
                              else f"session end (exit {e.payload.get('exit_code')})",
                              event_ids=[e.id], cumulative_tokens=total_tokens))
        # llm_request / note: not surfaced as steps
    close_open()
    return steps


def files_at(events: Iterable[Event], upto_seq: int | None = None) -> dict[str, str | None]:
    """Reconstruct {path: blob sha} for the working tree after event ``upto_seq``.

    Only files touched during the session are known; untouched files are
    implicitly unchanged from the original tree.
    """
    state: dict[str, str | None] = {}
    for e in sorted(events, key=lambda e: e.seq or 0):
        if upto_seq is not None and (e.seq or 0) > upto_seq:
            break
        for s in e.snapshots:
            state[s.path] = s.after
    return state
