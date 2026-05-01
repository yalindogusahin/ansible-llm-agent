"""Provider-agnostic LLM client for ansible_ai (native tool-use).

Each client speaks tool-use natively; the orchestrator drives a loop of
LLM -> tool_call -> tool_result -> LLM. The internal message format is
Anthropic-style (content blocks for text / tool_use / tool_result) and
each non-Anthropic client converts on the way in/out.

  Anthropic Messages       (tools, content blocks)         - native
  AWS Bedrock              (anthropic.* via InvokeModel)   - native (Anthropic shape)
  OpenAI Chat Completions  (tools, tool_calls)             - converted
  Ollama /api/chat         (tools, tool_calls)             - converted, model-dependent

Auth comes from env vars; nothing is logged. Provider is selected by
task arg `provider` > env ANSIBLE_AI_PROVIDER > "claude" default.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODELS = {
    "claude": "claude-opus-4-7",
    "openai": "gpt-4o",
    "bedrock": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "ollama": "llama3.1",
}


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Completion:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str = ""


class LLMClient(ABC):
    name: str = ""

    def __init__(
        self,
        model: str | None = None,
        timeout: int = 60,
        endpoint: str | None = None,
        api_key: str | None = None,
    ):
        self.model = model or DEFAULT_MODELS[self.name]
        self.timeout = timeout
        self.endpoint = endpoint
        self.api_key = api_key

    @abstractmethod
    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> Completion: ...

    def _post_json(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise LLMError(f"{self.name} HTTP {e.code}: {detail[:500]}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"{self.name} URL error: {e.reason}") from e


# Anthropic / Bedrock --------------------------------------------------------


def _parse_anthropic_response(payload: dict[str, Any]) -> Completion:
    """Pull text + tool_use blocks out of an Anthropic-shape response."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in payload.get("content", []):
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    input=block.get("input", {}) or {},
                )
            )
    usage = payload.get("usage", {})
    return Completion(
        text="".join(text_parts),
        tool_calls=tool_calls,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
        stop_reason=payload.get("stop_reason", ""),
    )


class ClaudeClient(LLMClient):
    name = "claude"

    def complete(self, system, messages, tools, max_tokens):
        api_key = (
            self.api_key
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        )
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN not set")
        base = self.endpoint or os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        url = base.rstrip("/") + "/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            # Cache the static parts of the prompt: system + tool definitions.
            # Per-host messages downstream of these markers are not cached.
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": messages,
        }
        if tools:
            body["tools"] = _cache_marked_tools(tools)
        resp = self._post_json(url, headers, body)
        return _parse_anthropic_response(resp)


def _cache_marked_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark the last tool with cache_control so the entire tools array is cached.

    Anthropic's prompt-caching docs: caching applies to all content up to and
    including the marker, so a single marker on the final tool covers them all.
    """
    if not tools:
        return tools
    out = [dict(t) for t in tools]
    out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
    return out


class BedrockClient(LLMClient):
    name = "bedrock"

    def complete(self, system, messages, tools, max_tokens):
        try:
            import boto3
        except ImportError as e:
            raise LLMError("bedrock provider requires boto3") from e

        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        client = boto3.client("bedrock-runtime", region_name=region)
        body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            # Cache markers on system + tools. Bedrock-hosted Claude models
            # honor the same cache_control field as the direct Anthropic API
            # for supported regions/models; on regions that don't support it
            # the field is ignored.
            "system": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": messages,
        }
        if tools:
            body["tools"] = _cache_marked_tools(tools)
        resp = client.invoke_model(
            modelId=self.model,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )
        payload = json.loads(resp["body"].read())
        return _parse_anthropic_response(payload)


# OpenAI ---------------------------------------------------------------------


def _to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic-style content blocks into OpenAI chat messages."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        blocks = content or []
        if role == "user":
            texts: list[str] = []
            for block in blocks:
                btype = block.get("type")
                if btype == "tool_result":
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": str(block.get("content", "")),
                        }
                    )
                elif btype == "text":
                    texts.append(block.get("text", ""))
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
        elif role == "assistant":
            texts = []
            tool_calls: list[dict[str, Any]] = []
            for block in blocks:
                btype = block.get("type")
                if btype == "text":
                    texts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {}) or {}),
                            },
                        }
                    )
            entry: dict[str, Any] = {"role": "assistant"}
            if texts:
                entry["content"] = "\n".join(texts)
            if tool_calls:
                entry["tool_calls"] = tool_calls
            # OpenAI requires assistant messages to carry content OR tool_calls.
            # Omit content when tool_calls are present (some compatible servers
            # reject `content: null`); fall back to "" only when neither exists.
            if "content" not in entry and "tool_calls" not in entry:
                entry["content"] = ""
            out.append(entry)
    return out


def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


def _parse_openai_response(payload: dict[str, Any]) -> Completion:
    choice = payload["choices"][0]
    msg = choice.get("message", {}) or {}
    text = msg.get("content") or ""
    tool_calls: list[ToolCall] = []
    for tc in msg.get("tool_calls", []) or []:
        fn = tc.get("function", {}) or {}
        try:
            args = json.loads(fn.get("arguments", "{}"))
        except (TypeError, json.JSONDecodeError):
            args = {}
        tool_calls.append(
            ToolCall(
                id=tc.get("id", ""),
                name=fn.get("name", ""),
                input=args,
            )
        )
    usage = payload.get("usage", {})
    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
    return Completion(
        text=text,
        tool_calls=tool_calls,
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
        cache_read_tokens=cached,
        stop_reason=choice.get("finish_reason", ""),
    )


class OpenAIClient(LLMClient):
    name = "openai"

    def complete(self, system, messages, tools, max_tokens):
        api_key = self.api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise LLMError("OPENAI_API_KEY not set")
        base = self.endpoint or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        url = base.rstrip("/") + "/chat/completions"
        headers = {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
        oa_messages = [{"role": "system", "content": system}] + _to_openai_messages(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": oa_messages,
        }
        if tools:
            body["tools"] = _to_openai_tools(tools)
        resp = self._post_json(url, headers, body)
        return _parse_openai_response(resp)


# Ollama ---------------------------------------------------------------------


def _to_ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic-style content blocks into Ollama chat messages.

    Ollama's /api/chat looks OpenAI-shaped but diverges in two places that
    matter for multi-turn tool use:
      - tool_calls[].function.arguments is a *dict*, not a JSON-encoded string
      - tool result messages have no tool_call_id (results are bound positionally)
    Sending OpenAI-shape arguments-as-string trips Ollama's JSON parser
    ('Value looks like object, but can't find closing }').
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        blocks = content or []
        if role == "user":
            texts: list[str] = []
            for block in blocks:
                btype = block.get("type")
                if btype == "tool_result":
                    out.append(
                        {"role": "tool", "content": str(block.get("content", ""))}
                    )
                elif btype == "text":
                    texts.append(block.get("text", ""))
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
        elif role == "assistant":
            texts = []
            tool_calls: list[dict[str, Any]] = []
            for block in blocks:
                btype = block.get("type")
                if btype == "text":
                    texts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append(
                        {
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": block.get("input", {}) or {},
                            }
                        }
                    )
            entry: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts)}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
    return out


class OllamaClient(LLMClient):
    name = "ollama"

    def complete(self, system, messages, tools, max_tokens):
        base = self.endpoint or os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
        url = base.rstrip("/") + "/api/chat"
        headers = {"content-type": "application/json"}
        ol_messages = [{"role": "system", "content": system}] + _to_ollama_messages(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "messages": ol_messages,
            "options": {"num_predict": max_tokens},
        }
        if tools:
            body["tools"] = _to_openai_tools(tools)
        resp = self._post_json(url, headers, body)
        msg = resp.get("message", {}) or {}
        text = msg.get("content", "") or ""
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {}) or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            elif not isinstance(args, dict):
                args = {}
            tool_calls.append(
                ToolCall(
                    id=tc.get("id", "") or fn.get("name", ""),
                    name=fn.get("name", ""),
                    input=args,
                )
            )
        return Completion(
            text=text,
            tool_calls=tool_calls,
            input_tokens=resp.get("prompt_eval_count", 0),
            output_tokens=resp.get("eval_count", 0),
            stop_reason=resp.get("done_reason", ""),
        )


_REGISTRY: dict[str, type[LLMClient]] = {
    "claude": ClaudeClient,
    "anthropic": ClaudeClient,
    "openai": OpenAIClient,
    "ollama": OllamaClient,
    "bedrock": BedrockClient,
}


def get_client(
    provider: str | None = None,
    model: str | None = None,
    timeout: int = 60,
    endpoint: str | None = None,
    api_key: str | None = None,
) -> LLMClient:
    p = (provider or os.environ.get("ANSIBLE_AI_PROVIDER") or "claude").lower()
    if p not in _REGISTRY:
        raise LLMError(f"unknown provider: {p}; known: {sorted(_REGISTRY)}")
    return _REGISTRY[p](model=model, timeout=timeout, endpoint=endpoint, api_key=api_key)
