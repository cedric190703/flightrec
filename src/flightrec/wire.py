"""Pure parsers for LLM wire formats -> flightrec events.

Supports Anthropic Messages (JSON + SSE) and OpenAI Chat Completions
(JSON + SSE) and OpenAI Responses (JSON + SSE). Anything else is recorded
as a raw llm_request/llm_response pair so nothing is lost.

The parsers are stateless; ``Dedup`` tracks what was already emitted across
requests, since every request re-sends the whole conversation history.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .events import Event, Kind, Source


@dataclass
class Dedup:
    seen_user: set[str] = field(default_factory=set)
    seen_results: set[str] = field(default_factory=set)
    seen_calls: set[str] = field(default_factory=set)

    def first(self, bucket: set[str], key: str) -> bool:
        if key in bucket:
            return False
        bucket.add(key)
        return True


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _clip(s: Any, n: int = 4000) -> Any:
    if isinstance(s, str) and len(s) > n:
        return s[:n] + f"… [+{len(s) - n} chars]"
    return s


def detect_provider(path: str, headers: dict[str, str]) -> str:
    p = path.lower()
    if "/v1/messages" in p or "x-api-key" in {k.lower() for k in headers}:
        return "anthropic"
    if "/chat/completions" in p:
        return "openai-chat"
    if "/responses" in p:
        return "openai-responses"
    return "unknown"


# --------------------------------------------------------------------------
# SSE reassembly
# --------------------------------------------------------------------------

def parse_sse(text: str) -> list[dict]:
    """Return the JSON ``data:`` payloads of an SSE stream, in order."""
    out: list[dict] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data_lines = [ln[5:].strip() for ln in block.split("\n") if ln.startswith("data:")]
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        if data == "[DONE]":
            continue
        try:
            out.append(json.loads(data))
        except json.JSONDecodeError:
            continue
    return out


def anthropic_sse_to_message(events: list[dict]) -> dict:
    """Fold Anthropic streaming events into the equivalent non-streamed message."""
    msg: dict = {"content": [], "usage": {}, "stop_reason": None, "model": None}
    blocks: dict[int, dict] = {}
    partial: dict[int, str] = {}
    for ev in events:
        t = ev.get("type")
        if t == "message_start":
            m = ev.get("message", {})
            msg["model"] = m.get("model")
            msg["usage"].update(m.get("usage", {}))
        elif t == "content_block_start":
            i = ev["index"]
            blocks[i] = dict(ev["content_block"])
            if blocks[i].get("type") == "text":
                blocks[i]["text"] = blocks[i].get("text", "")
            partial[i] = ""
        elif t == "content_block_delta":
            i = ev["index"]
            d = ev.get("delta", {})
            if d.get("type") == "text_delta":
                blocks[i]["text"] = blocks[i].get("text", "") + d.get("text", "")
            elif d.get("type") == "input_json_delta":
                partial[i] += d.get("partial_json", "")
            elif d.get("type") == "thinking_delta":
                blocks[i]["thinking"] = blocks[i].get("thinking", "") + d.get("thinking", "")
        elif t == "content_block_stop":
            i = ev["index"]
            if blocks.get(i, {}).get("type") == "tool_use":
                try:
                    blocks[i]["input"] = json.loads(partial[i] or "{}")
                except json.JSONDecodeError:
                    blocks[i]["input"] = {"_raw": partial[i]}
        elif t == "message_delta":
            msg["stop_reason"] = ev.get("delta", {}).get("stop_reason")
            msg["usage"].update(ev.get("usage", {}))
    msg["content"] = [blocks[i] for i in sorted(blocks)]
    return msg


def openai_chat_sse_to_message(chunks: list[dict]) -> dict:
    msg: dict = {"content": "", "tool_calls": [], "usage": {}, "model": None, "finish_reason": None}
    calls: dict[int, dict] = {}
    for ch in chunks:
        msg["model"] = ch.get("model") or msg["model"]
        if ch.get("usage"):
            msg["usage"] = ch["usage"]
        for choice in ch.get("choices", []):
            delta = choice.get("delta", {})
            if delta.get("content"):
                msg["content"] += delta["content"]
            for tc in delta.get("tool_calls", []) or []:
                i = tc.get("index", 0)
                slot = calls.setdefault(i, {"id": None, "name": "", "arguments": ""})
                slot["id"] = tc.get("id") or slot["id"]
                fn = tc.get("function", {})
                slot["name"] += fn.get("name", "") or ""
                slot["arguments"] += fn.get("arguments", "") or ""
            if choice.get("finish_reason"):
                msg["finish_reason"] = choice["finish_reason"]
    msg["tool_calls"] = [calls[i] for i in sorted(calls)]
    return msg


def openai_responses_sse_to_message(events: list[dict]) -> dict:
    for ev in reversed(events):
        if ev.get("type") in ("response.completed", "response.done") and "response" in ev:
            return ev["response"]
    # Fall back to assembling output items from item.done events.
    out = [ev["item"] for ev in events if ev.get("type") == "response.output_item.done"]
    return {"output": out, "usage": {}}


# --------------------------------------------------------------------------
# Event extraction
# --------------------------------------------------------------------------

def _json_or_none(text: str) -> dict | None:
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def extract(provider: str, path: str, req_body: str, resp_body: str,
            status: int, streamed: bool, dedup: Dedup, ts_req: float, ts_resp: float
            ) -> list[Event]:
    req = _json_or_none(req_body) or {}
    events: list[Event] = []

    request_ev = Event(Kind.LLM_REQUEST, Source.PROXY, {
        "provider": provider, "path": path, "model": req.get("model"),
        "stream": bool(req.get("stream")), "n_messages": None, "tools": [],
    }, ts=ts_req)
    events.append(request_ev)

    if provider == "anthropic":
        events += _anthropic(req, resp_body, streamed, dedup, request_ev, ts_req, ts_resp)
    elif provider == "openai-chat":
        events += _openai_chat(req, resp_body, streamed, dedup, request_ev, ts_req, ts_resp)
    elif provider == "openai-responses":
        events += _openai_responses(req, resp_body, streamed, dedup, request_ev, ts_req, ts_resp)
    else:
        events.append(Event(Kind.LLM_RESPONSE, Source.PROXY, {
            "provider": provider, "status": status, "raw": _clip(resp_body, 2000),
        }, ts=ts_resp, links=[request_ev.id]))
    request_ev.payload["status"] = status
    return events


def _anthropic(req, resp_body, streamed, dedup, request_ev, ts_req, ts_resp):
    events: list[Event] = []
    msgs = req.get("messages", [])
    request_ev.payload["n_messages"] = len(msgs)
    request_ev.payload["tools"] = [t.get("name") for t in req.get("tools", []) if isinstance(t, dict)]

    # Inputs: new user text and tool results carried in the history.
    for m in msgs:
        if m.get("role") != "user":
            continue
        content = m.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text"):
                key = _h(b["text"])
                if dedup.first(dedup.seen_user, key):
                    events.append(Event(Kind.USER_MESSAGE, Source.PROXY,
                                        {"text": _clip(b["text"])}, ts=ts_req, links=[request_ev.id]))
            elif b.get("type") == "tool_result":
                tid = b.get("tool_use_id", "")
                if dedup.first(dedup.seen_results, tid):
                    c = b.get("content")
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                    events.append(Event(Kind.TOOL_RESULT, Source.PROXY, {
                        "tool_use_id": tid, "is_error": bool(b.get("is_error")),
                        "content": _clip(c),
                    }, ts=ts_req, links=[tid]))

    # Output
    if streamed:
        msg = anthropic_sse_to_message(parse_sse(resp_body))
    else:
        msg = _json_or_none(resp_body) or {}
    text = "".join(b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text")
    thinking = "".join(b.get("thinking", "") for b in msg.get("content", []) if b.get("type") == "thinking")
    usage = msg.get("usage", {}) or {}
    resp_ev = Event(Kind.LLM_RESPONSE, Source.PROXY, {
        "provider": "anthropic", "model": msg.get("model"), "stop_reason": msg.get("stop_reason"),
        "text": _clip(text), "thinking": _clip(thinking, 2000),
        "usage": {"input": usage.get("input_tokens"), "output": usage.get("output_tokens"),
                  "cache_read": usage.get("cache_read_input_tokens"),
                  "cache_write": usage.get("cache_creation_input_tokens")},
        "latency": round(ts_resp - ts_req, 3),
    }, ts=ts_resp, links=[request_ev.id])
    events.append(resp_ev)
    for b in msg.get("content", []):
        if b.get("type") == "tool_use" and dedup.first(dedup.seen_calls, b.get("id", "")):
            ev = Event(Kind.TOOL_CALL, Source.PROXY, {
                "tool_use_id": b.get("id"), "name": b.get("name"), "input": b.get("input", {}),
            }, ts=ts_resp, links=[resp_ev.id])
            ev.id = b.get("id") or ev.id   # so tool_result can link by the wire id
            events.append(ev)
    return events


def _openai_chat(req, resp_body, streamed, dedup, request_ev, ts_req, ts_resp):
    events: list[Event] = []
    msgs = req.get("messages", [])
    request_ev.payload["n_messages"] = len(msgs)
    request_ev.payload["tools"] = [t.get("function", {}).get("name") for t in req.get("tools", [])
                                   if isinstance(t, dict)]
    for m in msgs:
        role = m.get("role")
        if role == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
            if c and dedup.first(dedup.seen_user, _h(c)):
                events.append(Event(Kind.USER_MESSAGE, Source.PROXY, {"text": _clip(c)},
                                    ts=ts_req, links=[request_ev.id]))
        elif role == "tool":
            tid = m.get("tool_call_id", "")
            if dedup.first(dedup.seen_results, tid):
                events.append(Event(Kind.TOOL_RESULT, Source.PROXY, {
                    "tool_use_id": tid, "is_error": False, "content": _clip(m.get("content")),
                }, ts=ts_req, links=[tid]))

    if streamed:
        msg = openai_chat_sse_to_message(parse_sse(resp_body))
        text, calls, usage = msg["content"], msg["tool_calls"], msg["usage"]
        model, finish = msg["model"], msg["finish_reason"]
    else:
        body = _json_or_none(resp_body) or {}
        choice = (body.get("choices") or [{}])[0]
        m = choice.get("message", {})
        text = m.get("content") or ""
        calls = [{"id": tc.get("id"), "name": tc.get("function", {}).get("name"),
                  "arguments": tc.get("function", {}).get("arguments", "")}
                 for tc in m.get("tool_calls", []) or []]
        usage, model, finish = body.get("usage", {}), body.get("model"), choice.get("finish_reason")
    resp_ev = Event(Kind.LLM_RESPONSE, Source.PROXY, {
        "provider": "openai-chat", "model": model, "stop_reason": finish, "text": _clip(text),
        "usage": {"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens")},
        "latency": round(ts_resp - ts_req, 3),
    }, ts=ts_resp, links=[request_ev.id])
    events.append(resp_ev)
    for c in calls:
        if dedup.first(dedup.seen_calls, c.get("id") or ""):
            args = _json_or_none(c.get("arguments") or "{}")
            ev = Event(Kind.TOOL_CALL, Source.PROXY, {
                "tool_use_id": c.get("id"), "name": c.get("name"),
                "input": args if args is not None else {"_raw": c.get("arguments")},
            }, ts=ts_resp, links=[resp_ev.id])
            ev.id = c.get("id") or ev.id
            events.append(ev)
    return events


def _openai_responses(req, resp_body, streamed, dedup, request_ev, ts_req, ts_resp):
    events: list[Event] = []
    inp = req.get("input", [])
    if isinstance(inp, str):
        inp = [{"role": "user", "content": inp}]
    request_ev.payload["n_messages"] = len(inp)
    request_ev.payload["tools"] = [t.get("name") for t in req.get("tools", []) if isinstance(t, dict)]
    for item in inp:
        if not isinstance(item, dict):
            continue
        if item.get("role") == "user":
            c = item.get("content")
            if isinstance(c, list):
                c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
            if c and dedup.first(dedup.seen_user, _h(c)):
                events.append(Event(Kind.USER_MESSAGE, Source.PROXY, {"text": _clip(c)},
                                    ts=ts_req, links=[request_ev.id]))
        elif item.get("type") == "function_call_output":
            tid = item.get("call_id", "")
            if dedup.first(dedup.seen_results, tid):
                events.append(Event(Kind.TOOL_RESULT, Source.PROXY, {
                    "tool_use_id": tid, "is_error": False, "content": _clip(item.get("output")),
                }, ts=ts_req, links=[tid]))

    body = openai_responses_sse_to_message(parse_sse(resp_body)) if streamed \
        else (_json_or_none(resp_body) or {})
    text = ""
    calls = []
    for item in body.get("output", []) or []:
        if item.get("type") == "message":
            text += "".join(c.get("text", "") for c in item.get("content", []) if isinstance(c, dict))
        elif item.get("type") == "function_call":
            calls.append(item)
    usage = body.get("usage", {}) or {}
    resp_ev = Event(Kind.LLM_RESPONSE, Source.PROXY, {
        "provider": "openai-responses", "model": body.get("model"), "stop_reason": body.get("status"),
        "text": _clip(text),
        "usage": {"input": usage.get("input_tokens"), "output": usage.get("output_tokens")},
        "latency": round(ts_resp - ts_req, 3),
    }, ts=ts_resp, links=[request_ev.id])
    events.append(resp_ev)
    for c in calls:
        cid = c.get("call_id") or c.get("id") or ""
        if dedup.first(dedup.seen_calls, cid):
            args = _json_or_none(c.get("arguments") or "{}")
            ev = Event(Kind.TOOL_CALL, Source.PROXY, {
                "tool_use_id": cid, "name": c.get("name"),
                "input": args if args is not None else {"_raw": c.get("arguments")},
            }, ts=ts_resp, links=[resp_ev.id])
            ev.id = cid or ev.id
            events.append(ev)
    return events
