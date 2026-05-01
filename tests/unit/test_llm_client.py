from __future__ import annotations

import json
from typing import Any

import pytest
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import llm_client as lmod


def test_get_client_unknown_provider_raises():
    with pytest.raises(lmod.LLMError):
        lmod.get_client(provider="not-a-real-thing")


def test_get_client_default_picks_claude(monkeypatch):
    monkeypatch.delenv("ANSIBLE_AI_PROVIDER", raising=False)
    c = lmod.get_client()
    assert c.name == "claude"


def test_get_client_env_selects_provider(monkeypatch):
    monkeypatch.setenv("ANSIBLE_AI_PROVIDER", "ollama")
    c = lmod.get_client()
    assert c.name == "ollama"


def test_get_client_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("ANSIBLE_AI_PROVIDER", "ollama")
    c = lmod.get_client(provider="openai")
    assert c.name == "openai"


def test_get_client_passes_endpoint_and_api_key():
    c = lmod.get_client(provider="openai", endpoint="http://x:8000/v1", api_key="k")
    assert c.endpoint == "http://x:8000/v1"
    assert c.api_key == "k"


def test_get_client_passes_model():
    c = lmod.get_client(provider="ollama", model="llama3.2")
    assert c.model == "llama3.2"


def test_default_models_have_known_providers():
    for p in ("claude", "openai", "bedrock", "ollama"):
        assert p in lmod.DEFAULT_MODELS


def test_anthropic_aliased_to_claude():
    c = lmod.get_client(provider="anthropic")
    assert c.name == "claude"


# --- response parsing -------------------------------------------------------


def test_parse_anthropic_response_text_only():
    resp = {
        "content": [{"type": "text", "text": "hello world"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "stop_reason": "end_turn",
    }
    c = lmod._parse_anthropic_response(resp)
    assert c.text == "hello world"
    assert c.tool_calls == []
    assert c.input_tokens == 10
    assert c.output_tokens == 5


def test_parse_anthropic_response_extracts_tool_use():
    resp = {
        "content": [
            {"type": "text", "text": "I'll check ports."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "run_cmd",
                "input": {"argv": ["ss", "-tlnp"], "reason": "check ports"},
            },
        ],
        "usage": {"input_tokens": 50, "output_tokens": 30, "cache_read_input_tokens": 200},
        "stop_reason": "tool_use",
    }
    c = lmod._parse_anthropic_response(resp)
    assert c.text == "I'll check ports."
    assert len(c.tool_calls) == 1
    tc = c.tool_calls[0]
    assert tc.id == "toolu_1"
    assert tc.name == "run_cmd"
    assert tc.input["argv"] == ["ss", "-tlnp"]
    assert c.cache_read_tokens == 200
    assert c.stop_reason == "tool_use"


def test_parse_openai_response_extracts_tool_calls():
    resp = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "run_cmd",
                                "arguments": json.dumps({"argv": ["ss"], "reason": "x"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 50},
        },
    }
    c = lmod._parse_openai_response(resp)
    assert c.text == ""
    assert len(c.tool_calls) == 1
    assert c.tool_calls[0].name == "run_cmd"
    assert c.tool_calls[0].input == {"argv": ["ss"], "reason": "x"}
    assert c.cache_read_tokens == 50


def test_parse_openai_response_handles_malformed_args():
    resp = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "x",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{not json"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    c = lmod._parse_openai_response(resp)
    assert c.tool_calls[0].input == {}


# --- message conversion -----------------------------------------------------


def test_to_openai_messages_passes_string_content():
    msgs = [{"role": "user", "content": "hi"}]
    out = lmod._to_openai_messages(msgs)
    assert out == [{"role": "user", "content": "hi"}]


def test_to_openai_messages_splits_tool_results_into_role_tool():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "exit=0\nout"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "exit=1"},
            ],
        }
    ]
    out = lmod._to_openai_messages(msgs)
    assert len(out) == 2
    assert out[0] == {"role": "tool", "tool_call_id": "t1", "content": "exit=0\nout"}
    assert out[1] == {"role": "tool", "tool_call_id": "t2", "content": "exit=1"}


def test_to_openai_messages_assistant_with_tool_use_becomes_tool_calls():
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking"},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "run_cmd",
                    "input": {"argv": ["ss"]},
                },
            ],
        }
    ]
    out = lmod._to_openai_messages(msgs)
    assert len(out) == 1
    entry = out[0]
    assert entry["role"] == "assistant"
    assert entry["content"] == "checking"
    assert len(entry["tool_calls"]) == 1
    tc = entry["tool_calls"][0]
    assert tc["id"] == "t1"
    assert tc["function"]["name"] == "run_cmd"
    assert json.loads(tc["function"]["arguments"]) == {"argv": ["ss"]}


def test_to_openai_messages_assistant_with_only_tool_use_omits_content_key():
    """OpenAI-compatible servers (vLLM, etc.) can reject `content: null`.
    When there's no text and there ARE tool_calls, omit the content key entirely.
    """
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "run_cmd", "input": {"argv": ["ss"]}},
            ],
        }
    ]
    out = lmod._to_openai_messages(msgs)
    assert len(out) == 1
    entry = out[0]
    assert "content" not in entry
    assert len(entry["tool_calls"]) == 1


def test_to_openai_messages_empty_assistant_falls_back_to_empty_content():
    """An assistant turn with neither text nor tool_use shouldn't disappear -
    OpenAI requires content or tool_calls, so we emit an empty string."""
    msgs = [{"role": "assistant", "content": []}]
    out = lmod._to_openai_messages(msgs)
    assert out == [{"role": "assistant", "content": ""}]


def test_to_ollama_messages_keeps_arguments_as_dict():
    """Regression: Ollama's chat API treats tool_calls[].function.arguments as
    a dict, not a JSON-encoded string. Sending a string trips its parser
    ('Value looks like object, but can't find closing }')."""
    msgs = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "run_cmd",
                    "input": {"argv": ["uname", "-r"], "reason": "x"},
                },
            ],
        }
    ]
    out = lmod._to_ollama_messages(msgs)
    assert len(out) == 1
    args = out[0]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(args, dict)
    assert args == {"argv": ["uname", "-r"], "reason": "x"}


def test_to_ollama_messages_tool_result_has_no_tool_call_id():
    """Ollama binds tool results positionally; tool_call_id is not part of its schema."""
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "exit=0"},
            ],
        }
    ]
    out = lmod._to_ollama_messages(msgs)
    assert out == [{"role": "tool", "content": "exit=0"}]


def test_to_openai_tools_wraps_in_function_envelope():
    tools = [
        {
            "name": "run_cmd",
            "description": "run a thing",
            "input_schema": {"type": "object", "properties": {"argv": {"type": "array"}}},
        }
    ]
    out = lmod._to_openai_tools(tools)
    assert out == [
        {
            "type": "function",
            "function": {
                "name": "run_cmd",
                "description": "run a thing",
                "parameters": {"type": "object", "properties": {"argv": {"type": "array"}}},
            },
        }
    ]


def test_cache_marked_tools_marks_only_last():
    tools = [
        {"name": "a", "description": "a", "input_schema": {}},
        {"name": "b", "description": "b", "input_schema": {}},
    ]
    out = lmod._cache_marked_tools(tools)
    assert "cache_control" not in out[0]
    assert out[1]["cache_control"] == {"type": "ephemeral"}
    # Source list untouched.
    assert "cache_control" not in tools[1]


def test_claude_complete_body_marks_system_and_tools_for_caching(monkeypatch):
    """Lock down the wire format: system has cache_control, last tool has cache_control."""
    captured: dict[str, Any] = {}

    def fake_post(self, url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        }

    monkeypatch.setattr(lmod.ClaudeClient, "_post_json", fake_post)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = lmod.ClaudeClient(model="claude-opus-4-7")
    tools = [
        {"name": "run_cmd", "description": "x", "input_schema": {}},
        {"name": "done", "description": "y", "input_schema": {}},
    ]
    client.complete("system text", [{"role": "user", "content": "hi"}], tools, max_tokens=10)
    body = captured["body"]
    assert isinstance(body["system"], list)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in body["tools"][0]
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_claude_complete_omits_tools_when_empty(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(self, url, headers, body):
        captured["body"] = body
        return {
            "content": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "stop_reason": "end_turn",
        }

    monkeypatch.setattr(lmod.ClaudeClient, "_post_json", fake_post)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = lmod.ClaudeClient()
    client.complete("sys", [{"role": "user", "content": "hi"}], None, max_tokens=10)
    assert "tools" not in captured["body"]
