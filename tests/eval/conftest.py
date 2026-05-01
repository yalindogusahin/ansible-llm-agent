"""Eval harness scaffolding.

Each YAML in tests/eval/fixtures/ describes one scripted investigation:
the prompt, rules, host context, an ordered list of LLM turns
(action JSON + the target's stdout/stderr/exit reply), and the diagnosis
expectations. The harness wires a ScriptedClient + scripted exec callable
into module_utils.orchestrator.run_agent and asserts the final shape.

The point: when the orchestrator (or the prompt schema, or the action
parser) is rewritten, regressions on a known-good investigation surface
as a failing fixture rather than a silent quality drop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@dataclass
class Fixture:
    name: str
    prompt: str
    rules: dict[str, Any]
    host_ctx: dict[str, Any]
    turns: list[dict[str, Any]]
    expect: dict[str, Any] = field(default_factory=dict)


def load_fixtures() -> list[Fixture]:
    items: list[Fixture] = []
    for path in sorted(FIXTURE_DIR.glob("*.yaml")):
        with path.open() as f:
            data = yaml.safe_load(f)
        items.append(
            Fixture(
                name=data["name"],
                prompt=data["prompt"],
                rules=data["rules"],
                host_ctx=data["host_ctx"],
                turns=data["turns"],
                expect=data.get("expect", {}),
            )
        )
    return items
