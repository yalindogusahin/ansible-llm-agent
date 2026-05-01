from __future__ import annotations

from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import rules as rmod
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import tools as tmod


def _rules(allow=None, deny=None):
    return rmod.merge(
        [
            {
                "allow": {
                    "run_cmd": allow.get("run_cmd", []) if allow else [],
                    "read_file": allow.get("read_file", []) if allow else [],
                    "write_file": allow.get("write_file", []) if allow else [],
                    "python": allow.get("python", []) if allow else [],
                    "network": allow.get("network", False) if allow else False,
                },
                "deny": {
                    "run_cmd": deny.get("run_cmd", []) if deny else [],
                    "read_file": deny.get("read_file", []) if deny else [],
                    "write_file": deny.get("write_file", []) if deny else [],
                    "python": deny.get("python", []) if deny else [],
                },
                "budget": {"max_iterations": 5, "max_tokens": 1000},
            }
        ]
    )


def _names(tools):
    return [t["name"] for t in tools]


def test_build_tools_emits_done_even_with_empty_allows():
    tools = tmod.build_tools(_rules())
    assert _names(tools) == [tmod.DONE]


def test_build_tools_run_cmd_only_when_allow_run_cmd_nonempty():
    tools = tmod.build_tools(_rules(allow={"run_cmd": ["ps"]}))
    assert tmod.RUN_CMD in _names(tools)
    assert tmod.READ_FILE not in _names(tools)
    assert tmod.RUN_PYTHON not in _names(tools)


def test_build_tools_run_python_gated_by_allow_python():
    no_py = tmod.build_tools(_rules(allow={"run_cmd": ["ps"]}))
    with_py = tmod.build_tools(_rules(allow={"run_cmd": ["ps"], "python": ["json"]}))
    assert tmod.RUN_PYTHON not in _names(no_py)
    assert tmod.RUN_PYTHON in _names(with_py)


def test_build_tools_run_cmd_description_lists_allowed_commands():
    tools = tmod.build_tools(_rules(allow={"run_cmd": ["ps", "ss", "cat"]}))
    desc = next(t["description"] for t in tools if t["name"] == tmod.RUN_CMD)
    assert "ps" in desc and "ss" in desc and "cat" in desc


def test_build_tools_read_file_description_lists_globs():
    tools = tmod.build_tools(_rules(allow={"read_file": ["/var/log/**"]}))
    desc = next(t["description"] for t in tools if t["name"] == tmod.READ_FILE)
    assert "/var/log/**" in desc


def test_exec_tool_run_cmd_blocks_disallowed_cmd():
    rules = _rules(allow={"run_cmd": ["ps"]})
    res = tmod.exec_tool(tmod.RUN_CMD, {"argv": ["rm", "-rf", "/"]}, rules)
    assert res.exit == 126
    assert "not allowed" in (res.blocked_by_rule or "")


def test_exec_tool_run_cmd_blocks_shell_dash_c():
    rules = _rules(allow={"run_cmd": ["bash"]})
    res = tmod.exec_tool(tmod.RUN_CMD, {"argv": ["bash", "-c", "rm -rf /"]}, rules)
    assert res.exit == 126
    assert "-c" in (res.blocked_by_rule or "")


def test_exec_tool_read_file_blocks_outside_allow():
    rules = _rules(allow={"read_file": ["/var/log/**"]})
    res = tmod.exec_tool(tmod.READ_FILE, {"path": "/etc/passwd"}, rules)
    assert res.exit == 126
    assert "not allowed" in (res.blocked_by_rule or "")


def test_exec_tool_read_file_blocks_shadow_even_when_etc_allowed():
    rules = _rules(allow={"read_file": ["/etc/**"]})
    res = tmod.exec_tool(tmod.READ_FILE, {"path": "/etc/shadow"}, rules)
    assert res.exit == 126
    assert "shadow" in (res.blocked_by_rule or "").lower() or "blocked" in (res.blocked_by_rule or "").lower()


def test_exec_tool_read_file_blocks_relative_path():
    rules = _rules(allow={"read_file": ["/var/log/**"]})
    res = tmod.exec_tool(tmod.READ_FILE, {"path": "var/log/foo"}, rules)
    assert res.exit == 126


def test_exec_tool_read_file_blocks_dotdot_traversal_to_shadow():
    """Regression: fnmatch alone matches `/var/log/../../etc/shadow` against
    the `/var/log/**` allow glob. Path canonicalization must run first so
    ALWAYS_BLOCK_READ catches the resolved /etc/shadow."""
    rules = _rules(allow={"read_file": ["/var/log/**"]})
    res = tmod.exec_tool(
        tmod.READ_FILE,
        {"path": "/var/log/../../etc/shadow"},
        rules,
    )
    assert res.exit == 126
    assert "shadow" in (res.blocked_by_rule or "").lower()


def test_exec_tool_write_file_blocks_dotdot_traversal_outside_allow():
    """Same canonicalization for write paths."""
    rules = _rules(allow={"write_file": ["/tmp/**"]})
    res = tmod.exec_tool(
        tmod.WRITE_FILE,
        {"path": "/tmp/../etc/passwd", "content": "x"},
        rules,
    )
    assert res.exit == 126
    assert "not allowed" in (res.blocked_by_rule or "")


def test_exec_tool_run_python_disabled_when_allow_python_empty():
    rules = _rules(allow={"run_cmd": ["ps"]})
    res = tmod.exec_tool(tmod.RUN_PYTHON, {"code": "print(1)"}, rules)
    assert res.exit == 126
    assert "run_python disabled" in (res.blocked_by_rule or "")


def test_exec_tool_run_python_blocks_disallowed_import():
    rules = _rules(allow={"run_cmd": [], "python": ["json"]})
    res = tmod.exec_tool(tmod.RUN_PYTHON, {"code": "import socket\nprint(1)"}, rules)
    assert res.exit == 126
    assert "import" in (res.blocked_by_rule or "")


def test_exec_tool_unknown_tool_returns_blocked():
    rules = _rules()
    res = tmod.exec_tool("nonsense", {}, rules)
    assert res.exit == 126
    assert "unknown tool" in (res.blocked_by_rule or "")


def test_exec_tool_write_file_requires_string_content():
    rules = _rules(allow={"write_file": ["/tmp/**"]})
    res = tmod.exec_tool(
        tmod.WRITE_FILE,
        {"path": "/tmp/test", "content": {"not": "a string"}},
        rules,
    )
    assert res.exit == 126
    assert "string" in (res.blocked_by_rule or "")
