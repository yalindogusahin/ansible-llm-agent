from __future__ import annotations

import pytest
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import rules as rmod
from ansible_collections.yalindogusahin.ansible_ai.plugins.module_utils import sandbox as smod


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


def test_validate_ast_rejects_os_system_outright():
    """os.system is rejected categorically: the runtime always invokes
    /bin/sh, so even a string whose first word is allow-listed slips arbitrary
    commands past the rule layer (`os.system('ls; rm -rf /')`)."""
    rules = _rules(allow_python=["os"], allow_run=["ls"])
    with pytest.raises(smod.SandboxViolation, match="always invokes a shell"):
        smod.validate_ast("import os\nos.system('ls -la')", rules)


def test_validate_ast_rejects_os_popen_outright():
    rules = _rules(allow_python=["os"], allow_run=["ls"])
    with pytest.raises(smod.SandboxViolation, match="always invokes a shell"):
        smod.validate_ast("import os\nos.popen('ls')", rules)


def test_validate_ast_string_argv_takes_first_word_via_subprocess():
    """The split-string convenience still works for subprocess.run; only the
    always-shell wrappers (os.system/popen) are categorically blocked."""
    rules = _rules(allow_python=["subprocess"], allow_run=["ls"])
    smod.validate_ast("import subprocess\nsubprocess.run('ls -la')", rules)


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


def test_validate_ast_rejects_bash_dash_c_list_form():
    rules = _rules(allow_python=["subprocess"], allow_run=["bash"])
    code = "import subprocess\nsubprocess.run(['bash', '-c', 'rm -rf /'])"
    with pytest.raises(smod.SandboxViolation, match="-c"):
        smod.validate_ast(code, rules)


def test_validate_ast_rejects_sh_dash_c_string_form_via_subprocess():
    """Reroute through subprocess.run since os.system is now categorically
    blocked; the -c rejection still fires for the split-string form."""
    rules = _rules(allow_python=["subprocess"], allow_run=["sh"])
    code = "import subprocess\nsubprocess.run('sh -c \"rm -rf /\"')"
    with pytest.raises(smod.SandboxViolation, match="-c"):
        smod.validate_ast(code, rules)


def test_validate_ast_rejects_bash_dash_c_with_glued_payload():
    rules = _rules(allow_python=["subprocess"], allow_run=["bash"])
    code = "import subprocess\nsubprocess.run(['bash', '-cevil'])"
    with pytest.raises(smod.SandboxViolation, match="-c"):
        smod.validate_ast(code, rules)


def test_validate_ast_rejects_absolute_shell_path_dash_c():
    rules = _rules(allow_python=["subprocess"], allow_run=["/bin/bash"])
    code = "import subprocess\nsubprocess.run(['/bin/bash', '-c', 'evil'])"
    with pytest.raises(smod.SandboxViolation, match="-c"):
        smod.validate_ast(code, rules)


def test_validate_ast_allows_bash_without_dash_c():
    rules = _rules(allow_python=["subprocess"], allow_run=["bash"])
    code = "import subprocess\nsubprocess.run(['bash', 'script.sh'])"
    smod.validate_ast(code, rules)


def test_validate_ast_rejects_bash_with_non_literal_tail():
    rules = _rules(allow_python=["subprocess"], allow_run=["bash"])
    code = "import subprocess\nargs = ['x']\nsubprocess.run(['bash', *args])"
    with pytest.raises(smod.SandboxViolation, match="non-literal"):
        smod.validate_ast(code, rules)


def test_validate_ast_rejects_subprocess_shell_true():
    """shell=True collapses argv into /bin/sh -c <cmd> and bypasses the
    allowlist entirely. Reject before the call is dispatched."""
    rules = _rules(allow_python=["subprocess"], allow_run=["ls"])
    code = "import subprocess\nsubprocess.run(['ls'], shell=True)"
    with pytest.raises(smod.SandboxViolation, match="shell=True"):
        smod.validate_ast(code, rules)


def test_validate_ast_rejects_subprocess_shell_dynamic():
    """A non-literal `shell=` kwarg can be flipped to True at runtime; reject
    anything that's not statically False."""
    rules = _rules(allow_python=["subprocess"], allow_run=["ls"])
    code = "import subprocess\nflag = True\nsubprocess.run(['ls'], shell=flag)"
    with pytest.raises(smod.SandboxViolation, match="statically False"):
        smod.validate_ast(code, rules)


def test_validate_ast_allows_subprocess_shell_false():
    """Explicit shell=False is fine and remains the recommended form."""
    rules = _rules(allow_python=["subprocess"], allow_run=["ls"])
    code = "import subprocess\nsubprocess.run(['ls'], shell=False)"
    smod.validate_ast(code, rules)


def test_validate_ast_rejects_getattr_eval_indirection():
    """getattr(__builtins__, 'eval') is the canonical sandbox-escape primitive:
    `eval` is a string here so the Name-based DANGEROUS_BUILTINS check misses it.
    Cover it with a literal-second-arg check on getattr/setattr/hasattr."""
    rules = _rules()
    with pytest.raises(smod.SandboxViolation, match="dangerous attribute"):
        smod.validate_ast("getattr(__builtins__, 'eval')('1+1')", rules)


def test_validate_ast_rejects_getattr_dunder_import():
    rules = _rules()
    with pytest.raises(smod.SandboxViolation, match="dangerous attribute"):
        smod.validate_ast("getattr({}, '__class__')", rules)


def test_validate_ast_rejects_setattr_dunder():
    rules = _rules()
    with pytest.raises(smod.SandboxViolation, match="dangerous attribute"):
        smod.validate_ast("setattr(o, '__class__', T)", rules)


def test_validate_ast_rejects_bare_builtins_name():
    """Even before the getattr lookup, `__builtins__` itself shouldn't be
    referenced; legitimate code never names it."""
    rules = _rules()
    with pytest.raises(smod.SandboxViolation, match="__builtins__"):
        smod.validate_ast("x = __builtins__", rules)


def test_validate_ast_allows_innocent_getattr():
    """getattr with an innocent literal second arg must still pass."""
    rules = _rules()
    smod.validate_ast("getattr(x, 'name', 'default')", rules)


def test_validate_ast_rejects_eval_inside_nested_function():
    """ast.walk recurses into function bodies, so a delayed `eval(...)` is
    still caught at parse time, not just when reached at runtime."""
    rules = _rules()
    with pytest.raises(smod.SandboxViolation, match="eval"):
        smod.validate_ast("def later():\n    eval('1+1')\n", rules)


def test_validate_ast_rejects_eval_via_alias():
    """Aliasing `eval` to a different name still trips the Name walker on
    the right-hand side of the assignment."""
    rules = _rules()
    with pytest.raises(smod.SandboxViolation, match="eval"):
        smod.validate_ast("e = eval\ne('1+1')", rules)


def test_validate_ast_rejects_subprocess_shell_true_via_check_output():
    """Cover all subprocess.X variants, not just .run/.Popen."""
    rules = _rules(allow_python=["subprocess"], allow_run=["ls"])
    code = "import subprocess\nsubprocess.check_output(['ls'], shell=True)"
    with pytest.raises(smod.SandboxViolation, match="shell=True"):
        smod.validate_ast(code, rules)


def test_shell_binaries_set_covers_common_shells():
    assert {"sh", "bash", "zsh", "dash"}.issubset(smod.SHELL_BINARIES)


def test_run_capped_returns_stdout_for_small_output():
    res = smod._run_capped(["/bin/echo", "hello world"], timeout=5)
    assert res.exit == 0
    assert res.timed_out is False
    assert "hello world" in res.stdout
    assert res.stderr == ""


def test_run_capped_truncates_oversized_stdout():
    cap = 1024
    res = smod._run_capped(
        ["/bin/sh", "-c", "yes hi | head -c 10000"],
        timeout=10,
        cap=cap,
    )
    assert res.exit == 0
    assert "[truncated at" in res.stdout
    # stdout = capped bytes + truncation marker; strict but bounded.
    assert len(res.stdout.encode("utf-8")) <= cap + 64


def test_run_capped_marks_timeout_and_kills_process():
    res = smod._run_capped(["/bin/sleep", "10"], timeout=1)
    assert res.timed_out is True
    assert res.exit == 124


def test_run_capped_captures_stderr_separately():
    res = smod._run_capped(
        ["/bin/sh", "-c", "echo out; echo err 1>&2"],
        timeout=5,
    )
    assert res.exit == 0
    assert "out" in res.stdout
    assert "err" in res.stderr
