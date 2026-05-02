# Security Policy

## Threat model

`ansible_ai` runs LLM-issued tool calls on operator-trusted target hosts.
The trust boundary is the rule set: an LLM is allowed to issue any tool
call within `allow.run_cmd` / `allow.read_file` / `allow.write_file` /
`allow.python`, but **not** anything outside it. Calls outside the
allow-list (or matching the deny-list) are rejected at the controller, at
the AST validator, and at the sandbox launcher (defense in depth).

A bug that lets an LLM-generated argv reach the target outside the rule
set is a security issue. So is a bypass of the AST validator (e.g.
`bash -c <payload>` slipping past the shell-`-c` rejection), a path
traversal through `read_file` / `write_file`, or a sandbox escape that
runs target-side code without the configured isolation tool.

Operator-supplied input (the `prompt`, the `rules` task arg, the
`endpoint` URL) is **trusted** - the operator can already run anything on
their own targets. Vulnerabilities that require an attacker to control
both the rules AND the prompt are out of scope.

## Supported versions

The latest released minor version is supported. Earlier minors receive
security fixes only when the issue cannot be reasonably mitigated by
upgrading. See `galaxy.yml` for the current version.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for a security report.

Use GitHub's private vulnerability reporting:
<https://github.com/yalindogusahin/ansible-ai-agent/security/advisories/new>

Or email the maintainer listed in `galaxy.yml` (`authors`) directly.
Include:

- a minimal reproduction (rules dict + prompt + observed bypass),
- the collection version (`galaxy.yml` `version`),
- the LLM provider/model used to trigger it, if relevant.

You should expect an acknowledgement within 7 days and a fix or status
update within 30 days. Coordinated disclosure is appreciated; please give
us a reasonable window before publishing a write-up.
