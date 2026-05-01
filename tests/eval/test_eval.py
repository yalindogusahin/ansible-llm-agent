"""Run scripted-LLM investigations against the orchestrator.

Each fixture gets one parametrized test. The test:
  1. wraps the fixture's turns in a ScriptedClient (one tool-use completion per turn)
  2. wraps the fixture's per-turn target replies in a scripted exec callable
  3. drives orchestrator.run_agent
  4. asserts diagnosis content + iteration count
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import (
    llm_client as lmod,
)
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import (
    orchestrator as omod,
)
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import (
    rules as rmod,
)

from .conftest import Fixture, load_fixtures


class ScriptedClient(lmod.LLMClient):
    """LLMClient stand-in that yields pre-canned tool-use completions in order."""

    name = "scripted"

    def __init__(self, turns: list[dict[str, Any]]):
        self.turns = list(turns)
        self.idx = 0
        self.model = "scripted"
        self.timeout = 0
        self.endpoint = None
        self.api_key = None

    def complete(self, system, messages, tools, max_tokens):  # noqa: ARG002
        if self.idx >= len(self.turns):
            raise lmod.LLMError("scripted client exhausted")
        turn = self.turns[self.idx]
        self.idx += 1
        call = turn["tool_call"]
        return lmod.Completion(
            text=turn.get("text", ""),
            tool_calls=[
                lmod.ToolCall(
                    id=f"call_{self.idx}",
                    name=call["name"],
                    input=call.get("input", {}),
                )
            ],
            input_tokens=turn.get("tokens", {}).get("input", 50),
            output_tokens=turn.get("tokens", {}).get("output", 50),
        )


def _scripted_exec(turns: list[dict[str, Any]]):
    """Returns a callable yielding each non-`done` turn's `target` reply in order."""
    state = {"idx": 0}

    def call(name, inp, rules, timeout):  # noqa: ARG001
        i = state["idx"]
        state["idx"] += 1
        if i >= len(turns):
            return {"stdout": "", "stderr": "scripted exec exhausted", "exit": 1, "blocked_by_rule": None}
        target = turns[i].get("target") or {}
        return {
            "stdout": target.get("stdout", ""),
            "stderr": target.get("stderr", ""),
            "exit": target.get("exit", 0),
            "blocked_by_rule": target.get("blocked_by_rule"),
        }

    return call


@pytest.mark.parametrize("fixture", load_fixtures(), ids=lambda f: f.name)
def test_eval_fixture(fixture: Fixture):
    rules = rmod.merge([fixture.rules])
    client = ScriptedClient(fixture.turns)
    # Only the non-done turns advance the exec callable.
    exec_turns = [t for t in fixture.turns if t["tool_call"]["name"] != "done"]
    exec_call = _scripted_exec(exec_turns)

    out = omod.run_agent(
        prompt=fixture.prompt,
        rules=rules,
        host_ctx=fixture.host_ctx,
        llm_client=client,
        exec_callable=exec_call,
        timeout=10,
    )

    diag = out["diagnosis"]
    expect = fixture.expect

    for needle in expect.get("diagnosis_contains", []):
        assert needle.lower() in diag.lower(), (
            f"diagnosis missing expected substring {needle!r}; got: {diag!r}"
        )

    for pattern in expect.get("diagnosis_matches", []):
        assert re.search(pattern, diag), (
            f"diagnosis did not match pattern {pattern!r}; got: {diag!r}"
        )

    if "max_iterations" in expect:
        assert out["iterations_used"] <= expect["max_iterations"], (
            f"used {out['iterations_used']} iterations, "
            f"expected <= {expect['max_iterations']}"
        )

    if expect.get("must_converge", True):
        assert not diag.startswith("stopped:"), f"agent did not converge: {diag}"
        assert not diag.startswith("LLM error"), f"LLM error: {diag}"
