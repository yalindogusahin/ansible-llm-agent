from __future__ import annotations

from typing import Any

from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import (
    llm_client as lmod,
)
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import (
    orchestrator as omod,
)
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import (
    rules as rmod,
)


def _rules():
    return rmod.merge(
        [
            {
                "allow": {
                    "run_cmd": ["ss", "ps"],
                    "read_file": [],
                    "write_file": [],
                    "python": [],
                    "network": False,
                },
                "deny": {"run_cmd": [], "read_file": [], "write_file": [], "python": []},
                "budget": {"max_iterations": 5, "max_tokens": 8000},
            }
        ]
    )


def _host_ctx():
    return {
        "hostname": "h1",
        "groups": ["g"],
        "role": "g",
        "facts": {"ansible_distribution": "Ubuntu"},
        "hostvars": {},
    }


class _ScriptedClient(lmod.LLMClient):
    name = "scripted"

    def __init__(self, completions):
        self.completions = list(completions)
        self.idx = 0
        self.model = "scripted"
        self.timeout = 0
        self.endpoint = None
        self.api_key = None

    def complete(self, system, messages, tools, max_tokens):  # noqa: ARG002
        c = self.completions[self.idx]
        self.idx += 1
        return c


def _ok_target(*_args, **_kwargs):
    return {"stdout": "out", "stderr": "", "exit": 0, "blocked_by_rule": None}


def test_run_agent_emits_on_step_for_each_action():
    completions = [
        lmod.Completion(
            text="",
            tool_calls=[lmod.ToolCall(id="1", name="run_cmd", input={"argv": ["ss", "-tlnp"], "reason": "x"})],
            input_tokens=10,
            output_tokens=5,
        ),
        lmod.Completion(
            text="",
            tool_calls=[lmod.ToolCall(id="2", name="done", input={"summary": "found", "reason": "y"})],
            input_tokens=10,
            output_tokens=5,
        ),
    ]
    seen: list[dict[str, Any]] = []
    out = omod.run_agent(
        prompt="p",
        rules=_rules(),
        host_ctx=_host_ctx(),
        llm_client=_ScriptedClient(completions),
        exec_callable=_ok_target,
        on_step=lambda e: seen.append(e),
    )
    assert out["diagnosis"] == "found"
    actions = [e.get("action") for e in seen]
    assert actions == ["run_cmd", "done"]


def test_run_agent_swallows_on_step_exceptions():
    completions = [
        lmod.Completion(
            tool_calls=[lmod.ToolCall(id="1", name="done", input={"summary": "ok", "reason": "y"})],
            input_tokens=1,
            output_tokens=1,
        ),
    ]

    def boom(_entry):
        raise RuntimeError("display broke")

    out = omod.run_agent(
        prompt="p",
        rules=_rules(),
        host_ctx=_host_ctx(),
        llm_client=_ScriptedClient(completions),
        exec_callable=_ok_target,
        on_step=boom,
    )
    assert out["diagnosis"] == "ok"


def test_run_agent_text_only_completion_falls_back_to_text():
    completions = [
        lmod.Completion(text="here is what I found", tool_calls=[], input_tokens=1, output_tokens=1),
    ]
    out = omod.run_agent(
        prompt="p",
        rules=_rules(),
        host_ctx=_host_ctx(),
        llm_client=_ScriptedClient(completions),
        exec_callable=_ok_target,
    )
    assert "here is what I found" in out["diagnosis"]


def test_run_agent_token_budget_stops_loop():
    big_completion = lmod.Completion(
        tool_calls=[lmod.ToolCall(id="1", name="run_cmd", input={"argv": ["ss"], "reason": "x"})],
        input_tokens=99999,
        output_tokens=99999,
    )
    out = omod.run_agent(
        prompt="p",
        rules=_rules(),
        host_ctx=_host_ctx(),
        llm_client=_ScriptedClient([big_completion]),
        exec_callable=_ok_target,
    )
    assert "token budget" in out["diagnosis"]


def test_run_agent_rejects_unknown_tool_and_recovers():
    """Unknown tool name on iter 1, valid done on iter 2 -> diagnosis succeeds."""
    completions = [
        lmod.Completion(
            tool_calls=[lmod.ToolCall(id="1", name="hack_db", input={"q": "drop"})],
            input_tokens=5,
            output_tokens=5,
        ),
        lmod.Completion(
            tool_calls=[lmod.ToolCall(id="2", name="done", input={"summary": "fine", "reason": "y"})],
            input_tokens=5,
            output_tokens=5,
        ),
    ]
    out = omod.run_agent(
        prompt="p",
        rules=_rules(),
        host_ctx=_host_ctx(),
        llm_client=_ScriptedClient(completions),
        exec_callable=_ok_target,
    )
    assert out["diagnosis"] == "fine"
    bad_step = out["transcript"][0]
    assert bad_step["action"] == "hack_db"
    assert "unknown tool" in bad_step["blocked_by_rule"]


def test_run_agent_rejects_run_cmd_missing_argv():
    """run_cmd without argv is malformed; orchestrator should refuse to dispatch."""
    completions = [
        lmod.Completion(
            tool_calls=[lmod.ToolCall(id="1", name="run_cmd", input={"reason": "x"})],
            input_tokens=5,
            output_tokens=5,
        ),
        lmod.Completion(
            tool_calls=[lmod.ToolCall(id="2", name="done", input={"summary": "k", "reason": "y"})],
            input_tokens=5,
            output_tokens=5,
        ),
    ]
    out = omod.run_agent(
        prompt="p",
        rules=_rules(),
        host_ctx=_host_ctx(),
        llm_client=_ScriptedClient(completions),
        exec_callable=_ok_target,
    )
    assert out["diagnosis"] == "k"
    assert "argv" in out["transcript"][0]["blocked_by_rule"]


def test_run_agent_aborts_after_two_consecutive_bad_iterations():
    """Two iterations of only-malformed calls -> loop exits with diagnostic."""
    bad = lmod.Completion(
        tool_calls=[lmod.ToolCall(id="1", name="garbage_tool", input={})],
        input_tokens=5,
        output_tokens=5,
    )
    out = omod.run_agent(
        prompt="p",
        rules=_rules(),
        host_ctx=_host_ctx(),
        llm_client=_ScriptedClient([bad, bad, bad]),
        exec_callable=_ok_target,
    )
    assert "invalid tool calls" in out["diagnosis"]


def test_run_agent_aggregates_cache_tokens():
    completions = [
        lmod.Completion(
            tool_calls=[lmod.ToolCall(id="1", name="done", input={"summary": "ok", "reason": "y"})],
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=900,
            cache_write_tokens=100,
        ),
    ]
    out = omod.run_agent(
        prompt="p",
        rules=_rules(),
        host_ctx=_host_ctx(),
        llm_client=_ScriptedClient(completions),
        exec_callable=_ok_target,
    )
    tu = out["tokens_used"]
    assert tu["cache_read"] == 900
    assert tu["cache_write"] == 100
