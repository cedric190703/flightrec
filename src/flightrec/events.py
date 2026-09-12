"""Universal, harness-neutral event model.

Every observer (fs watcher, exec shim, LLM proxy, native adapters) emits
``Event`` objects. The ``kind`` vocabulary is deliberately small so that a
viewer written against it works for any harness.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Kind(StrEnum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    USER_MESSAGE = "user_message"      # what the human typed (if observable)
    LLM_REQUEST = "llm_request"        # request to a model API
    LLM_RESPONSE = "llm_response"      # response from a model API
    TOOL_CALL = "tool_call"            # model asked the harness to do something
    TOOL_RESULT = "tool_result"        # harness reported back to the model
    FS_CHANGE = "fs_change"            # file created / modified / deleted
    EXEC = "exec"                      # a shell command was run
    NOTE = "note"                      # free-form annotation


class Source(StrEnum):
    FS = "fs"
    SHIM = "shim"
    PROXY = "proxy"
    CLI = "cli"
    CORRELATOR = "correlator"
    # native adapters use "adapter:<harness>" as a plain string


@dataclass(slots=True)
class Snapshot:
    """Before/after content hashes for a file touched by an event."""

    path: str
    before: str | None  # blob sha256, None if the file did not exist
    after: str | None   # blob sha256, None if the file was deleted


@dataclass(slots=True)
class Event:
    kind: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    seq: int | None = None                      # assigned by the store on append
    links: list[str] = field(default_factory=list)  # ids of causally related events
    snapshots: list[Snapshot] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "Event":
        d = json.loads(line)
        d["snapshots"] = [Snapshot(**s) for s in d.get("snapshots", [])]
        return cls(**d)
