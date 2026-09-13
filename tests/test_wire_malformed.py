"""Wire parsers against bodies that are *shaped* wrong, and provider paths
not covered elsewhere (OpenAI Responses SSE, non-streamed chat tool calls,
list-content messages, tools lists, cross-turn call dedup)."""

import json

from flightrec.events import Kind
from flightrec.wire import (Dedup, _dicts, _text_of, extract, openai_chat_sse_to_message,
                            openai_responses_sse_to_message)


def _kinds(evs):
    return [e.kind for e in evs]


def test_dicts_and_text_of_helpers():
    assert _dicts([{"a": 1}, "x", None, 3]) == [{"a": 1}]
    assert _dicts("str") == [] and _dicts(None) == [] and _dicts({"a": 1}) == []
    assert _text_of("plain") == "plain"
    assert _text_of([{"type": "text", "text": "a"}, {"type": "image"}, "junk"]) == "a"
    assert _text_of([{"type": "text", "text": "a"}, {"type": "text", "text": 5}, {"text": "b"}]) == "a\nb"
    assert _text_of(None) == ""


def test_anthropic_content_string_instead_of_list_keeps_request():
    req = {"model": "m", "messages": [{"role": "user", "content": "q"}]}
    resp = {"content": "not-a-list", "usage": "nope", "model": "m"}
    evs = extract("anthropic", "/v1/messages", json.dumps(req), json.dumps(resp),
                  200, False, Dedup(), 1.0, 2.0)
    assert _kinds(evs) == [Kind.LLM_REQUEST, Kind.USER_MESSAGE, Kind.LLM_RESPONSE]
    assert evs[0].payload["status"] == 200
    assert evs[-1].payload["text"] == "" and evs[-1].payload["usage"]["input"] is None


def test_anthropic_messages_not_a_list():
    req = {"model": "m", "messages": "hello", "tools": "none"}
    evs = extract("anthropic", "/v1/messages", json.dumps(req), "{}", 200, False, Dedup(), 0, 1)
    assert evs[0].payload["n_messages"] == 0 and evs[0].payload["tools"] == []
    assert _kinds(evs) == [Kind.LLM_REQUEST, Kind.LLM_RESPONSE]


def test_parse_exception_yields_raw_response_and_note(monkeypatch):
    import flightrec.wire as wire

    def boom(*a, **k):
        raise RuntimeError("synthetic")

    monkeypatch.setitem(wire._PARSERS, "anthropic", boom)
    evs = extract("anthropic", "/v1/messages", "{}", "<body>", 500, False, Dedup(), 0, 1)
    assert _kinds(evs) == [Kind.LLM_REQUEST, Kind.LLM_RESPONSE, Kind.NOTE]
    assert evs[0].payload["status"] == 500
    assert evs[1].payload["raw"] == "<body>" and evs[1].links == [evs[0].id]
    assert "synthetic" in evs[2].payload["error"] and evs[2].links == [evs[0].id]


def test_openai_chat_choices_wrong_shape():
    resp = {"choices": "none", "usage": None, "model": "gpt"}
    evs = extract("openai-chat", "/v1/chat/completions", "{}", json.dumps(resp),
                  200, False, Dedup(), 0, 1)
    assert _kinds(evs) == [Kind.LLM_REQUEST, Kind.LLM_RESPONSE]
    assert evs[1].payload["model"] == "gpt" and evs[1].payload["usage"] == {"input": None, "output": None}


def test_openai_chat_non_streamed_tool_calls_and_tools_list():
    req = {"model": "gpt", "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
           "tools": [{"type": "function", "function": {"name": "run"}}, {"type": "function"}]}
    resp = {"model": "gpt", "choices": [{"finish_reason": "tool_calls", "message": {
        "content": None,
        "tool_calls": [{"id": "call_a", "function": {"name": "run", "arguments": "{\"x\": 1}"}},
                       {"id": "call_b", "function": {"name": "run", "arguments": "{broken"}}]}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3}}
    evs = extract("openai-chat", "/v1/chat/completions", json.dumps(req), json.dumps(resp),
                  200, False, Dedup(), 0, 1)
    assert evs[0].payload["tools"] == ["run", None]
    assert _kinds(evs) == [Kind.LLM_REQUEST, Kind.USER_MESSAGE, Kind.LLM_RESPONSE,
                           Kind.TOOL_CALL, Kind.TOOL_CALL]
    assert evs[1].payload["text"] == "hi"
    assert evs[3].id == "call_a" and evs[3].payload["input"] == {"x": 1}
    assert evs[4].payload["input"] == {"_raw": "{broken"}
    assert evs[2].payload["stop_reason"] == "tool_calls" and evs[2].payload["text"] == ""


def test_openai_chat_sse_without_tool_calls_or_usage():
    chunks = [{"model": "g", "choices": [{"delta": {"content": "He"}}]},
              {"choices": [{"delta": {"content": "llo"}, "finish_reason": "stop"}]},
              {"choices": [{"delta": {"tool_calls": None}}]}]
    m = openai_chat_sse_to_message(chunks)
    assert m == {"content": "Hello", "tool_calls": [], "usage": {}, "model": "g", "finish_reason": "stop"}


def test_openai_responses_streamed_uses_completed_event():
    events = [
        {"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "c0",
                                                        "name": "ignored", "arguments": "{}"}},
        {"type": "response.completed", "response": {
            "model": "gpt", "status": "completed", "usage": {"input_tokens": 4, "output_tokens": 6},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "hey"}]},
                       {"type": "function_call", "call_id": "c1", "name": "sh", "arguments": "{\"c\":1}"}]}},
    ]
    sse = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
    req = {"model": "gpt", "input": [{"role": "user", "content": [{"type": "input_text", "text": "go"}]},
                                     "junk", {"type": "function_call_output", "call_id": "prev", "output": "42"}]}
    evs = extract("openai-responses", "/v1/responses", json.dumps(req), sse, 200, True, Dedup(), 0, 1)
    assert _kinds(evs) == [Kind.LLM_REQUEST, Kind.USER_MESSAGE, Kind.TOOL_RESULT,
                           Kind.LLM_RESPONSE, Kind.TOOL_CALL]
    assert evs[0].payload["n_messages"] == 2           # the junk string is not counted
    assert evs[2].payload["tool_use_id"] == "prev" and evs[2].payload["content"] == "42"
    assert evs[3].payload["text"] == "hey" and evs[3].payload["usage"] == {"input": 4, "output": 6}
    assert evs[4].id == "c1" and evs[4].payload["input"] == {"c": 1}


def test_openai_responses_sse_fallback_assembles_items():
    events = [{"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "c9",
                                                              "name": "f", "arguments": "{}"}},
              {"type": "response.output_text.delta", "delta": "x"}]
    m = openai_responses_sse_to_message(events)
    assert [i["call_id"] for i in m["output"]] == ["c9"] and m["usage"] == {}


def test_openai_responses_output_wrong_shape():
    resp = {"output": "text", "usage": [], "model": "gpt", "status": "completed"}
    evs = extract("openai-responses", "/v1/responses", "{}", json.dumps(resp), 200, False, Dedup(), 0, 1)
    assert _kinds(evs) == [Kind.LLM_REQUEST, Kind.LLM_RESPONSE]
    assert evs[1].payload["text"] == ""


def test_anthropic_tool_call_not_re_emitted_across_turns():
    dedup = Dedup()
    resp = {"model": "m", "stop_reason": "tool_use", "usage": {},
            "content": [{"type": "tool_use", "id": "t1", "name": "f", "input": {}}]}
    first = extract("anthropic", "/v1/messages", "{}", json.dumps(resp), 200, False, dedup, 0, 1)
    second = extract("anthropic", "/v1/messages", "{}", json.dumps(resp), 200, False, dedup, 2, 3)
    assert Kind.TOOL_CALL in _kinds(first) and Kind.TOOL_CALL not in _kinds(second)


def test_anthropic_tool_result_list_content_and_error_flag():
    req = {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
         "content": [{"type": "text", "text": "line1"}, {"type": "image"}, "junk"]},
        {"type": "image", "source": {}},            # non-text user block is ignored
        {"type": "text", "text": ""},                # empty text is ignored
    ]}]}
    evs = extract("anthropic", "/v1/messages", json.dumps(req), "{}", 200, False, Dedup(), 0, 1)
    assert _kinds(evs) == [Kind.LLM_REQUEST, Kind.TOOL_RESULT, Kind.LLM_RESPONSE]
    r = evs[1].payload
    assert r["is_error"] is True and r["content"] == "line1" and evs[1].links == ["t1"]


def test_anthropic_user_text_non_string_is_ignored():
    req = {"messages": [{"role": "user", "content": [{"type": "text", "text": 123}]}]}
    evs = extract("anthropic", "/v1/messages", json.dumps(req), "{}", 200, False, Dedup(), 0, 1)
    assert Kind.USER_MESSAGE not in _kinds(evs)


def test_request_event_records_stream_flag_and_model():
    req = {"model": "claude-x", "stream": True, "messages": []}
    evs = extract("anthropic", "/v1/messages", json.dumps(req), "", 200, True, Dedup(), 0, 1)
    p = evs[0].payload
    assert p["model"] == "claude-x" and p["stream"] is True and p["path"] == "/v1/messages"


def test_failure_inside_a_real_parser_still_records_raw_body(monkeypatch):
    import flightrec.wire as wire

    real = wire._PARSERS["anthropic"]

    def half(req, resp_body, *rest):
        real(req, "{}", *rest)                       # the request side parses fine...
        raise RuntimeError("...then the response blows up")

    monkeypatch.setitem(wire._PARSERS, "anthropic", half)
    req = {"messages": [{"role": "user", "content": "keep me"}]}
    evs = extract("anthropic", "/v1/messages", json.dumps(req), "??", 200, False, Dedup(), 0, 1)
    assert _kinds(evs) == [Kind.LLM_REQUEST, Kind.LLM_RESPONSE, Kind.NOTE]
    assert evs[1].payload["raw"] == "??"
