"""Provider-agnostic LLM client for ansible_ai.

Implementations talk to:
  * Anthropic Claude Messages API
  * OpenAI Chat Completions API
  * AWS Bedrock (anthropic.claude-* via InvokeModel)
  * Ollama (local /api/chat)

All clients return a single response string for one user turn. The caller
is responsible for prompt accumulation and JSON parsing (see prompts.parse_action).

Auth comes from env vars; nothing is logged. Provider is selected by:
  task arg `provider` > env ANSIBLE_AI_PROVIDER > "claude" default.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
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
class Completion:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


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
    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int) -> Completion: ...

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


class ClaudeClient(LLMClient):
    name = "claude"

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int) -> Completion:
        api_key = (
            self.api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
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
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": messages,
        }
        resp = self._post_json(url, headers, body)
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        usage = resp.get("usage", {})
        return Completion(
            text=text,
            input_tokens=usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )


class OpenAIClient(LLMClient):
    name = "openai"

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int) -> Completion:
        api_key = self.api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise LLMError("OPENAI_API_KEY not set")
        base = self.endpoint or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        url = base.rstrip("/") + "/chat/completions"
        headers = {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}] + messages,
        }
        resp = self._post_json(url, headers, body)
        choice = resp["choices"][0]["message"].get("content") or ""
        usage = resp.get("usage", {})
        return Completion(
            text=choice,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )


class OllamaClient(LLMClient):
    name = "ollama"

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int) -> Completion:
        base = self.endpoint or os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
        url = base.rstrip("/") + "/api/chat"
        headers = {"content-type": "application/json"}
        body = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}] + messages,
            "options": {"num_predict": max_tokens},
        }
        resp = self._post_json(url, headers, body)
        msg = resp.get("message", {})
        return Completion(
            text=msg.get("content", ""),
            input_tokens=resp.get("prompt_eval_count", 0),
            output_tokens=resp.get("eval_count", 0),
        )


class BedrockClient(LLMClient):
    name = "bedrock"

    def complete(self, system: str, messages: list[dict[str, str]], max_tokens: int) -> Completion:
        try:
            import boto3
        except ImportError as e:
            raise LLMError("bedrock provider requires boto3") from e

        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        client = boto3.client("bedrock-runtime", region_name=region)
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        resp = client.invoke_model(
            modelId=self.model,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )
        payload = json.loads(resp["body"].read())
        text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
        usage = payload.get("usage", {})
        return Completion(
            text=text,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
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
