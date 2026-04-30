from __future__ import annotations

import pytest
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import rules as rmod


def _layer(allow=None, deny=None, budget=None):
    out = {"allow": {}, "deny": {}, "budget": {}}
    if allow:
        out["allow"] = allow
    if deny:
        out["deny"] = deny
    if budget:
        out["budget"] = budget
    return out


def test_validate_rejects_glob_in_run_cmd():
    with pytest.raises(rmod.RuleError):
        rmod.validate(_layer(allow={"run_cmd": ["rm*"]}))


def test_validate_rejects_relative_read_file():
    with pytest.raises(rmod.RuleError):
        rmod.validate(_layer(allow={"read_file": ["var/log/foo"]}))


def test_validate_rejects_unknown_allow_key():
    with pytest.raises(rmod.RuleError):
        rmod.validate({"allow": {"unknown": []}, "deny": {}, "budget": {}})


def test_validate_rejects_non_bool_network():
    with pytest.raises(rmod.RuleError):
        rmod.validate(_layer(allow={"network": "yes"}))


def test_validate_rejects_negative_budget():
    with pytest.raises(rmod.RuleError):
        rmod.validate(_layer(budget={"max_iterations": 0}))


def test_merge_unions_allow_lists():
    a = _layer(allow={"run_cmd": ["ps", "ls"]})
    b = _layer(allow={"run_cmd": ["ls", "cat"]})
    out = rmod.merge([a, b])
    assert sorted(out["allow"]["run_cmd"]) == ["cat", "ls", "ps"]


def test_merge_deny_wins_across_layers():
    a = _layer(allow={"run_cmd": ["rm", "ps"]})
    b = _layer(deny={"run_cmd": ["rm"]})
    out = rmod.merge([a, b])
    assert "rm" not in out["allow"]["run_cmd"]
    assert "rm" in out["deny"]["run_cmd"]
    assert "ps" in out["allow"]["run_cmd"]


def test_merge_higher_layer_overrides_network_flag():
    a = _layer(allow={"network": False})
    b = _layer(allow={"network": True})
    out = rmod.merge([a, b])
    assert out["allow"]["network"] is True


def test_merge_higher_layer_overrides_budget():
    a = _layer(budget={"max_iterations": 5, "max_tokens": 8000})
    b = _layer(budget={"max_iterations": 1})
    out = rmod.merge([a, b])
    assert out["budget"]["max_iterations"] == 1
    assert out["budget"]["max_tokens"] == 8000


def test_is_cmd_allowed_respects_deny():
    rules = rmod.merge([_layer(allow={"run_cmd": ["ps"]}, deny={"run_cmd": ["ps"]})])
    assert rmod.is_cmd_allowed(rules, "ps") is False


def test_is_cmd_allowed_unknown_is_false():
    rules = rmod.merge([_layer(allow={"run_cmd": ["ps"]})])
    assert rmod.is_cmd_allowed(rules, "rm") is False


def test_is_path_allowed_glob_match():
    rules = rmod.merge([_layer(allow={"read_file": ["/var/log/**"]})])
    assert rmod.is_path_allowed(rules, "/var/log/syslog", "read") is True
    assert rmod.is_path_allowed(rules, "/etc/passwd", "read") is False


def test_is_path_allowed_deny_overrides():
    rules = rmod.merge(
        [
            _layer(allow={"read_file": ["/etc/**"]}),
            _layer(deny={"read_file": ["/etc/shadow"]}),
        ]
    )
    assert rmod.is_path_allowed(rules, "/etc/shadow", "read") is False
    assert rmod.is_path_allowed(rules, "/etc/hosts", "read") is True


def test_is_python_import_allowed_supports_dotted_prefix():
    rules = rmod.merge([_layer(allow={"python": ["os.path"]})])
    assert rmod.is_python_import_allowed(rules, "os.path") is True
    assert rmod.is_python_import_allowed(rules, "os.path.join") is True
    assert rmod.is_python_import_allowed(rules, "socket") is False


def test_is_python_import_deny_blocks_subpackage():
    rules = rmod.merge(
        [
            _layer(allow={"python": ["os"]}),
            _layer(deny={"python": ["os.system"]}),
        ]
    )
    assert rmod.is_python_import_allowed(rules, "os.system") is False
    assert rmod.is_python_import_allowed(rules, "os.path") is True


def test_empty_layers_produces_safe_default():
    out = rmod.merge([])
    assert out["allow"]["run_cmd"] == []
    assert out["allow"]["network"] is False
    assert out["budget"]["max_iterations"] == 5
