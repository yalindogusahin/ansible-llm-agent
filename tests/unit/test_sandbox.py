from __future__ import annotations

import pytest

from ansible_collections.ysahin.ansible_ai.plugins.module_utils import rules as rmod
from ansible_collections.ysahin.ansible_ai.plugins.module_utils import sandbox as smod


def _rules(allow_python=None, allow_run=None, deny_run=None, deny_python=None):
    layer = {
        "allow": {
            "python": allow_python or [],
            "run_cmd": allow_run or [],
            "read_file": [],
            "write_file": [],
            "network": False,
        },
        "deny": {
            "python": deny_python or [],
            "run_cmd": deny_run or [],
            "read_file": [],
            "write_file": [],
        },
        "budget": {"max_iterations": 5, "max_tokens": 1000},
    }
    return rmod.merge([layer])


def test_validate_ast_allows_simple_print():
    smod.validate_ast("print('hello')", _rules())


def test_validate_ast_rejects_disallowed_import():
    with pytest.raises(smod.SandboxViolation):
        smod.validate_ast("import socket", _rules())


def test_validate_ast_allows_listed_import():
    rules = _rules(allow_python=["json"])
    smod.validate_ast("import json\nprint(json.dumps({'a':1}))", rules)


def test_validate_ast_rejects_eval():
    with pytest.raises(smod.SandboxViolation):
        smod.validate_ast("eval('1+1')", _rules())


def test_validate_ast_rejects_exec():
    with pytest.raises(smod.SandboxViolation):
        smod.validate_ast("exec('print(1)')", _rules())


def test_validate_ast_rejects_subprocess_with_denied_cmd():
    rules = _rules(allow_python=["subprocess"], deny_run=["rm"])
    code = "import subprocess\nsubprocess.run(['rm', '-rf', '/'])"
    with pytest.raises(smod.SandboxViolation):
        smod.validate_ast(code, rules)


def test_validate_ast_allows_subprocess_with_allowed_cmd():
    rules = _rules(allow_python=["subprocess"], allow_run=["ps"])
    code = "import subprocess\nsubprocess.run(['ps', 'aux'], capture_output=True)"
    smod.validate_ast(code, rules)


def test_validate_ast_rejects_dynamic_argv():
    rules = _rules(allow_python=["subprocess"], allow_run=["ps"])
    code = "import subprocess\ncmd = ['ps']\nsubprocess.run(cmd)"
    with pytest.raises(smod.SandboxViolation):
        smod.validate_ast(code, rules)


def test_validate_ast_rejects_os_system_denied_cmd():
    rules = _rules(allow_python=["os"], allow_run=[])
    with pytest.raises(smod.SandboxViolation):
        smod.validate_ast("import os\nos.system('rm -rf /')", rules)


def test_validate_ast_string_argv_takes_first_word():
    rules = _rules(allow_python=["os"], allow_run=["ls"])
    smod.validate_ast("import os\nos.system('ls -la')", rules)


def test_validate_ast_rejects_syntax_error():
    with pytest.raises(smod.SandboxViolation):
        smod.validate_ast("def broken(:", _rules())


def test_detect_isolation_returns_known_value():
    val = smod.detect_isolation()
    assert val in {"bwrap", "firejail", "nsjail", "rlimit"}


def test_dangerous_builtins_set_includes_eval_exec():
    assert "eval" in smod.DANGEROUS_BUILTINS
    assert "exec" in smod.DANGEROUS_BUILTINS
    assert "__import__" in smod.DANGEROUS_BUILTINS
