"""Edge cases for the wire parsers: clipping, thinking/cache usage,
malformed bodies, and history dedup across many turns."""

import json

from flightrec.events import Kind
from flightrec.wire import Dedup, _clip, _h, extract, parse_sse


def test_clip_truncates_long_strings_only():
    assert _clip("short") == "short"
    out = _clip("x" * 5000, n=100)
    assert out.startswith("x" * 100) and "+4900 chars" in out
    assert _clip({"k": "v"}) == {"k": "v"}          # non-strings pass through
    assert _clip(12345) == 12345


def test_hash_is_stable_and_short():
    assert _h("abc") == _h("abc") and len(_h("abc")) == 16


def test_anthropic_thinking_and_cache_tokens():
    resp = {
        "model": "claude-x", "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 20,
                  "cache_read_input_tokens": 80, "cache_creation_input_tokens": 10},
        "content": [
            {"type": "thinking", "thinking": "Let me reason about this."},
            {"type": "text", "text": "The answer is 42."},
        ],
    }
    req = {"model": "claude-x", "messages": [{"role": "user", "content": "q"}]}
    evs = extract("anthropic", "/v1/messages", json.dumps(req), json.dumps(resp),
                  200, False, Dedup(), 1.0, 2.0)
    resp_ev = next(e for e in evs if e.kind == Kind.LLM_RESPONSE)
    assert resp_ev.payload["text"] == "The answer is 42."
    assert resp_ev.payload["thinking"] == "Let me reason about this."
    assert resp_ev.payload["usage"] == {"input": 100, "output": 20, "cache_read": 80, "cache_write": 10}


def test_malformed_request_body_does_not_crash():
    evs = extract("anthropic", "/v1/messages", "{not json", "{}", 200, False, Dedup(), 0.0, 0.1)
    # Still emits a request + a (empty) response event rather than raising.
    assert evs[0].kind == Kind.LLM_REQUEST
    assert any(e.kind == Kind.LLM_RESPONSE for e in evs)
    assert evs[0].payload["n_messages"] == 0


def test_truncated_tool_use_json_is_kept_raw():
    sse = (
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"tool_use","id":"t1","name":"edit","input":{}}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":"{\\"path\\": \\"a"}}\n\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
    )
    req = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    evs = extract("anthropic", "/v1/messages", json.dumps(req), sse, 200, True, Dedup(), 0, 1)
    call = next(e for e in evs if e.kind == Kind.TOOL_CALL)
    assert "_raw" in call.payload["input"]           # invalid JSON preserved, not dropped


def test_dedup_emits_each_user_message_once_across_turns():
    dedup = Dedup()
    hist = [{"role": "user", "content": "first request"}]
    for turn in range(3):
        req = {"model": "m", "messages": list(hist)}
        resp = {"model": "m", "stop_reason": "end_turn", "usage": {},
                "content": [{"type": "text", "text": f"reply {turn}"}]}
        evs = extract("anthropic", "/v1/messages", json.dumps(req), json.dumps(resp),
                      200, False, dedup, float(turn), turn + 0.5)
        hist.append({"role": "assistant", "content": [{"type": "text", "text": f"reply {turn}"}]})
        hist.append({"role": "user", "content": f"follow up {turn}"})
        user_msgs = [e for e in evs if e.kind == Kind.USER_MESSAGE]
        # Turn 0 emits the seed message; later turns emit only the one new follow-up.
        assert len(user_msgs) == 1


def test_parse_sse_handles_crlf_and_multiline_data():
    stream = "data: {\"a\":\r\ndata: 1}\r\n\r\n"
    assert parse_sse(stream) == [{"a": 1}]
