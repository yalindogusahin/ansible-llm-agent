from __future__ import annotations

import pytest
from ansible_collections.ysahin.ansible_ai.plugins.module_utils import llm_client as lmod


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
