from __future__ import annotations

import pytest
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import prompts as pmod
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import rules as rmod


def _rules():
    return rmod.merge(
        [
            {
                "allow": {
                    "run_cmd": ["ps", "ss"],
                    "read_file": ["/var/log/**"],
                    "write_file": [],
                    "python": ["json", "subprocess"],
                    "network": False,
                },
                "deny": {
                    "run_cmd": ["rm"],
                    "read_file": [],
                    "write_file": ["**"],
                    "python": ["socket"],
                },
                "budget": {"max_iterations": 3, "max_tokens": 1000},
            }
        ]
    )


def _host_ctx():
    return {
        "hostname": "kafka-broker-1",
        "groups": ["kafka_broker", "all"],
        "role": "kafka_broker",
        "facts": {
            "ansible_distribution": "Ubuntu",
            "ansible_kernel": "6.5.0-test",
        },
        "hostvars": {},
    }


def test_build_system_prompt_includes_allow_lists():
    out = pmod.build_system_prompt("find broken connect", _rules(), _host_ctx())
    assert "ps" in out and "ss" in out
    assert "json" in out and "subprocess" in out
    assert "/var/log/**" in out


def test_build_system_prompt_includes_deny_lists():
    out = pmod.build_system_prompt("find broken connect", _rules(), _host_ctx())
    assert "rm" in out
    assert "socket" in out


def test_build_system_prompt_renders_host_context():
    out = pmod.build_system_prompt("find broken connect", _rules(), _host_ctx())
    assert "kafka-broker-1" in out
    assert "kafka_broker" in out
    assert "Ubuntu" in out


def test_build_system_prompt_renders_network_flag():
    out = pmod.build_system_prompt("p", _rules(), _host_ctx())
    assert "network egress: no" in out


def test_filter_hostvars_redacts_secrets():
    hv = {
        "db_url": "postgres://h:5432/d",
        "db_password": "synthetic-not-real",
        "api_token": "synthetic-not-real",
        "private_value": "synthetic-not-real",
    }
    filtered = pmod.filter_hostvars(hv)
    assert filtered["db_url"] == "postgres://h:5432/d"
    assert filtered["db_password"] == "<redacted>"
    assert filtered["api_token"] == "<redacted>"
    assert filtered["private_value"] == "<redacted>"


def test_filter_hostvars_redacts_nested_secret_keys():
    hv = {"creds": {"username": "u", "password": "synthetic"}}
    filtered = pmod.filter_hostvars(hv)
    assert filtered["creds"]["username"] == "u"
    assert filtered["creds"]["password"] == "<redacted>"


def test_filter_hostvars_drops_ansible_prefixed():
    hv = {"ansible_facts": {"x": 1}, "custom": 2}
    filtered = pmod.filter_hostvars(hv)
    assert "ansible_facts" not in filtered
    assert filtered["custom"] == 2


def test_filter_facts_keeps_default_keys():
    facts = {"ansible_distribution": "Ubuntu", "irrelevant": "x"}
    filtered = pmod.filter_facts(facts)
    assert filtered.get("ansible_distribution") == "Ubuntu"
    assert "irrelevant" not in filtered


def test_render_observation_includes_exit_and_streams():
    out = pmod.render_observation("hello\n", "warn\n", 0)
    assert "exit=0" in out
    assert "STDOUT" in out and "hello" in out
    assert "STDERR" in out and "warn" in out


def test_render_observation_includes_blocked_reason():
    out = pmod.render_observation("", "", 126, blocked="rm denied")
    assert "blocked_by_rule=rm denied" in out


def test_parse_action_run_python_ok():
    text = '{"action": "run_python", "code": "print(1)", "reason": "test"}'
    action = pmod.parse_action(text)
    assert action["action"] == "run_python"
    assert action["code"] == "print(1)"


def test_parse_action_done_ok():
    text = '{"action": "done", "summary": "found it", "reason": "x"}'
    action = pmod.parse_action(text)
    assert action["action"] == "done"
    assert action["summary"] == "found it"


def test_parse_action_strips_markdown_fence():
    text = '```json\n{"action": "done", "summary": "ok"}\n```'
    action = pmod.parse_action(text)
    assert action["action"] == "done"


def test_parse_action_extracts_from_chatty_output():
    text = 'Sure, here is the action:\n{"action": "done", "summary": "ok"}\nLet me know!'
    action = pmod.parse_action(text)
    assert action["action"] == "done"


def test_parse_action_rejects_unknown_action():
    with pytest.raises(ValueError):
        pmod.parse_action('{"action": "go", "code": "x"}')


def test_parse_action_rejects_missing_code_for_run_python():
    with pytest.raises(ValueError):
        pmod.parse_action('{"action": "run_python", "reason": "x"}')


def test_parse_action_rejects_garbage():
    with pytest.raises(ValueError):
        pmod.parse_action("not json at all")


def test_build_aggregate_prompt_with_dict_results():
    results = {
        "kafka-broker-1": {
            "diagnosis": "broker port 9092 not listening",
            "iterations_used": 3,
            "tokens_used": {"input": 1000, "output": 200},
        },
        "kafka-broker-2": {
            "diagnosis": "broker up but connect cannot resolve dns",
            "iterations_used": 4,
            "tokens_used": {"input": 1100, "output": 250},
        },
    }
    out = pmod.build_aggregate_prompt("Cluster summary?", results)
    assert "Cluster summary?" in out
    assert "kafka-broker-1" in out and "kafka-broker-2" in out
    assert "broker port 9092 not listening" in out
    assert "broker up but connect cannot resolve dns" in out
    assert "iterations=3" in out and "iterations=4" in out
    assert "action" in out and "done" in out


def test_build_aggregate_prompt_with_list_synthesizes_host_names():
    results = [
        {"diagnosis": "node A", "iterations_used": 1},
        {"diagnosis": "node B", "iterations_used": 2},
    ]
    out = pmod.build_aggregate_prompt("summarize", results)
    assert "host_0" in out and "host_1" in out
    assert "node A" in out and "node B" in out


def test_build_aggregate_prompt_handles_empty_results():
    out = pmod.build_aggregate_prompt("p", [])
    assert "(no per-host results provided)" in out


def test_build_aggregate_prompt_handles_non_dict_results():
    out = pmod.build_aggregate_prompt("p", "garbage-string-not-supported")
    assert "(no per-host results provided)" in out


def test_build_aggregate_prompt_truncates_huge_diagnosis():
    huge = "x" * 5000
    results = {"h": {"diagnosis": huge, "iterations_used": 1}}
    out = pmod.build_aggregate_prompt("p", results)
    assert "..." in out
    assert "x" * 5000 not in out


def test_build_aggregate_prompt_skips_non_dict_entries():
    results = {"good": {"diagnosis": "ok"}, "bad": "not-a-dict"}
    out = pmod.build_aggregate_prompt("p", results)
    assert "good" in out and "ok" in out
    assert "not-a-dict" not in out
