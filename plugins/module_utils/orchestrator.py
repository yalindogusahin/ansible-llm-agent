"""Pure ReAct orchestrator loop, decoupled from Ansible plumbing.

The loop drives a tool-use conversation:

  1. ask the LLM (system + tool definitions + messages so far)
  2. for each returned tool_use block:
       - `done`  -> capture summary, exit
       - other   -> hand off to exec_callable, collect result
  3. append the assistant turn + a user turn carrying tool_result blocks
  4. repeat until done, max_iterations, or token budget exceeded

`exec_callable(tool_name, tool_input, rules, timeout)` runs one tool call
on the target host. The action plugin wires it to ai_exec; the eval
harness wires it to a scripted dict.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from . import llm_client as llm_mod
from . import prompts as prompts_mod
from . import tools as tools_mod
from .sandbox import SandboxResult

ExecCallable = Callable[[str, dict[str, Any], dict[str, Any], int], dict[str, Any]]
StepCallback = Callable[[dict[str, Any]], None]


def _coerce_result(raw: Any) -> SandboxResult:
    """Accept either a SandboxResult or a plain dict (ai_exec returns dicts)."""
    if isinstance(raw, SandboxResult):
        return raw
    if not isinstance(raw, dict):
        return SandboxResult(stdout="", stderr=f"unexpected exec result: {raw!r}", exit=1)
    return SandboxResult(
        stdout=raw.get("stdout", ""),
        stderr=raw.get("stderr", ""),
        exit=raw.get("exit", -1),
        timed_out=raw.get("timed_out", False),
        blocked_by_rule=raw.get("blocked_by_rule"),
    )


def run_agent(
    prompt: str,
    rules: dict[str, Any],
    host_ctx: dict[str, Any],
    llm_client: llm_mod.LLMClient,
    exec_callable: ExecCallable,
    timeout: int = 30,
    on_step: StepCallback | None = None,
) -> dict[str, Any]:
    """Drive the tool-use loop. Returns diagnosis + transcript + token accounting.

    `on_step`, if provided, is invoked once per transcript entry (tool calls
    plus the final `done`). Used by the action plugin for live streaming.
    Failures inside on_step are swallowed - streaming must never break the loop.
    """

    def _emit(entry: dict[str, Any]) -> None:
        if on_step is None:
            return
        # Streaming must never break the loop - swallow display errors.
        with contextlib.suppress(Exception):
            on_step(entry)
    system = prompts_mod.build_system_prompt(prompt, rules, host_ctx)
    tools = tools_mod.build_tools(rules)

    transcript: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Begin the investigation. Use tools as needed and call `done` when finished."}
    ]
    diagnosis: str | None = None
    iterations = 0
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0
    max_iter = rules["budget"]["max_iterations"]
    max_tokens = rules["budget"]["max_tokens"]

    for iterations in range(1, max_iter + 1):
        try:
            completion = llm_client.complete(system, messages, tools, max_tokens=1024)
        except llm_mod.LLMError as e:
            entry = {"step": iterations, "error": f"llm: {e}"}
            transcript.append(entry)
            _emit(entry)
            diagnosis = f"LLM error before convergence: {e}"
            break

        total_input += completion.input_tokens
        total_output += completion.output_tokens
        total_cache_read += completion.cache_read_tokens
        total_cache_write += completion.cache_write_tokens
        if total_input + total_output > max_tokens:
            entry = {"step": iterations, "error": "token budget exceeded"}
            transcript.append(entry)
            _emit(entry)
            diagnosis = "stopped: token budget exceeded"
            break

        # Mirror the assistant turn into messages so the next call sees it.
        assistant_blocks: list[dict[str, Any]] = []
        if completion.text:
            assistant_blocks.append({"type": "text", "text": completion.text})
        for tc in completion.tool_calls:
            assistant_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.input,
                }
            )
        messages.append({"role": "assistant", "content": assistant_blocks})

        if not completion.tool_calls:
            # Model produced text without calling a tool. Treat as soft exit
            # using whatever text it gave us; nudge with a follow-up if empty.
            diagnosis = completion.text.strip() or "(no diagnosis - model returned no tool call and no text)"
            entry = {
                "step": iterations,
                "action": "text_only",
                "text": completion.text[:1000],
            }
            transcript.append(entry)
            _emit(entry)
            break

        # Process tool calls in order; `done` short-circuits.
        tool_results: list[dict[str, Any]] = []
        done_seen = False
        for tc in completion.tool_calls:
            if tc.name == tools_mod.DONE:
                diagnosis = tc.input.get("summary", "(no summary)")
                entry = {
                    "step": iterations,
                    "action": "done",
                    "summary": diagnosis,
                    "reason": tc.input.get("reason", ""),
                }
                transcript.append(entry)
                _emit(entry)
                done_seen = True
                break

            try:
                raw = exec_callable(tc.name, tc.input, rules, timeout)
            except Exception as e:  # noqa: BLE001 - propagate any failure as a tool error to the model
                raw = {"stdout": "", "stderr": f"exec error: {e}", "exit": 1, "blocked_by_rule": None}
            res = _coerce_result(raw)

            entry = {
                "step": iterations,
                "action": tc.name,
                "input": tc.input,
                "reason": tc.input.get("reason", ""),
                "stdout": res.stdout,
                "stderr": res.stderr,
                "exit": res.exit,
                "blocked_by_rule": res.blocked_by_rule,
            }
            transcript.append(entry)
            _emit(entry)

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": prompts_mod.render_tool_result(
                        res.stdout, res.stderr, res.exit, blocked=res.blocked_by_rule
                    ),
                    "is_error": res.blocked_by_rule is not None or res.exit != 0,
                }
            )

        if done_seen:
            break

        messages.append({"role": "user", "content": tool_results})
    else:
        diagnosis = "stopped: max_iterations reached without 'done'"

    return {
        "transcript": transcript,
        "diagnosis": diagnosis or "(no diagnosis)",
        "iterations_used": iterations,
        "tokens_used": {
            "input": total_input,
            "output": total_output,
            "cache_read": total_cache_read,
            "cache_write": total_cache_write,
        },
    }


def run_aggregate(
    prompt: str,
    results: Any,
    llm_client: llm_mod.LLMClient,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    """Single-shot cluster aggregation. One LLM call, only the `done` tool."""
    system = prompts_mod.build_aggregate_prompt(prompt, results)
    done_tool = [t for t in tools_mod.build_tools({"allow": {}, "deny": {}, "budget": {}}) if t["name"] == tools_mod.DONE]
    messages = [{"role": "user", "content": "Emit your cluster-level summary now."}]
    completion = llm_client.complete(system, messages, done_tool, max_tokens=max_tokens)

    summary: str | None = None
    for tc in completion.tool_calls:
        if tc.name == tools_mod.DONE:
            summary = tc.input.get("summary", "(no summary)")
            break
    if summary is None:
        # Some providers will produce a plain text answer instead of calling done;
        # accept that as the summary rather than failing the whole play.
        summary = completion.text.strip() or "(no summary)"

    host_count = len(results) if isinstance(results, dict | list) else 0
    return {
        "diagnosis": summary,
        "tokens_used": {
            "input": completion.input_tokens,
            "output": completion.output_tokens,
            "cache_read": completion.cache_read_tokens,
            "cache_write": completion.cache_write_tokens,
        },
        "host_count": host_count,
    }
