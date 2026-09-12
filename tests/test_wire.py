import json

from flightrec.events import Kind
from flightrec.wire import Dedup, detect_provider, extract, parse_sse


def _kinds(evs):
    return [e.kind for e in evs]


def test_detect_provider():
    assert detect_provider("/v1/messages", {}) == "anthropic"
    assert detect_provider("/v1/chat/completions", {}) == "openai-chat"
    assert detect_provider("/v1/responses", {}) == "openai-responses"
    assert detect_provider("/whatever", {}) == "unknown"


ANTHROPIC_SSE = """event: message_start
data: {"type":"message_start","message":{"model":"claude-x","usage":{"input_tokens":10}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Let me edit."}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: content_block_start
data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_1","name":"edit_file","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\": \\"a.py\\""}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":", \\"content\\": \\"x\\"}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":1}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":25}}

event: message_stop
data: {"type":"message_stop"}
"""


def test_anthropic_stream_then_tool_result():
    dedup = Dedup()
    req1 = {"model": "claude-x", "stream": True, "tools": [{"name": "edit_file"}],
            "messages": [{"role": "user", "content": "fix a.py"}]}
    evs = extract("anthropic", "/v1/messages", json.dumps(req1), ANTHROPIC_SSE,
                  200, True, dedup, 1.0, 2.0)
    assert _kinds(evs) == [Kind.LLM_REQUEST, Kind.USER_MESSAGE, Kind.LLM_RESPONSE, Kind.TOOL_CALL]
    call = evs[-1]
    assert call.id == "toolu_1"
    assert call.payload["name"] == "edit_file"
    assert call.payload["input"] == {"path": "a.py", "content": "x"}
    resp = evs[2]
    assert resp.payload["text"] == "Let me edit."
    assert resp.payload["usage"] == {"input": 10, "output": 25, "cache_read": None, "cache_write": None}
    assert resp.payload["latency"] == 1.0

    # Second turn re-sends history + the tool result; only the new result is emitted.
    req2 = {"model": "claude-x", "messages": [
        {"role": "user", "content": "fix a.py"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "edit_file", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
    ]}
    resp2 = {"model": "claude-x", "stop_reason": "end_turn", "usage": {"input_tokens": 50, "output_tokens": 5},
             "content": [{"type": "text", "text": "Done."}]}
    evs2 = extract("anthropic", "/v1/messages", json.dumps(req2), json.dumps(resp2),
                   200, False, dedup, 3.0, 3.5)
    assert _kinds(evs2) == [Kind.LLM_REQUEST, Kind.TOOL_RESULT, Kind.LLM_RESPONSE]
    assert evs2[1].links == ["toolu_1"]
    assert evs2[2].payload["text"] == "Done."


OPENAI_SSE = "\n\n".join([
    'data: {"model":"gpt-x","choices":[{"delta":{"role":"assistant","content":""}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"run_cmd","arguments":""}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"cmd\\": "}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"ls\\"}"}}]},"finish_reason":"tool_calls"}]}',
    'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}',
    "data: [DONE]",
]) + "\n\n"


def test_openai_chat_stream():
    dedup = Dedup()
    req = {"model": "gpt-x", "stream": True,
           "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "list files"}]}
    evs = extract("openai-chat", "/v1/chat/completions", json.dumps(req), OPENAI_SSE,
                  200, True, dedup, 0.0, 1.0)
    assert _kinds(evs) == [Kind.LLM_REQUEST, Kind.USER_MESSAGE, Kind.LLM_RESPONSE, Kind.TOOL_CALL]
    assert evs[-1].payload == {"tool_use_id": "call_1", "name": "run_cmd", "input": {"cmd": "ls"}}
    assert evs[2].payload["usage"] == {"input": 7, "output": 3}

    req2 = {"model": "gpt-x", "messages": req["messages"] + [
        {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "run_cmd", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "a.py"}]}
    resp2 = {"model": "gpt-x", "choices": [{"message": {"content": "There is a.py"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 20, "completion_tokens": 4}}
    evs2 = extract("openai-chat", "/v1/chat/completions", json.dumps(req2), json.dumps(resp2),
                   200, False, dedup, 2.0, 2.5)
    assert _kinds(evs2) == [Kind.LLM_REQUEST, Kind.TOOL_RESULT, Kind.LLM_RESPONSE]


def test_openai_responses_json():
    dedup = Dedup()
    req = {"model": "gpt-x", "input": "hello"}
    resp = {"model": "gpt-x", "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 2},
            "output": [{"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{\"cmd\":\"pwd\"}"}]}
    evs = extract("openai-responses", "/v1/responses", json.dumps(req), json.dumps(resp),
                  200, False, dedup, 0.0, 0.1)
    assert _kinds(evs) == [Kind.LLM_REQUEST, Kind.USER_MESSAGE, Kind.LLM_RESPONSE, Kind.TOOL_CALL]
    assert evs[-1].payload["input"] == {"cmd": "pwd"}


def test_unknown_provider_keeps_raw():
    evs = extract("unknown", "/x", "not json", "<html>", 404, False, Dedup(), 0, 1)
    assert _kinds(evs) == [Kind.LLM_REQUEST, Kind.LLM_RESPONSE]
    assert evs[1].payload["raw"] == "<html>"


def test_parse_sse_ignores_garbage():
    assert parse_sse("data: {\"a\":1}\n\ndata: nope\n\n: comment\n\n") == [{"a": 1}]
