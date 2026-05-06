# Contributing

Thanks for considering a contribution to `yalindogusahin.ansible_ai`. This
guide covers the local setup, the test/lint workflow, and the conventions
used in this repo so your PR lands smoothly.

## Development setup

```bash
git clone https://github.com/yalindogusahin/ansible-ai-agent
cd ansible-ai-agent
python3 -m venv .venv
source .venv/bin/activate
pip install pre-commit pytest pyyaml ansible-core boto3 ruff
pre-commit install   # hook fires automatically before each commit
```

Once `pre-commit install` has run, every `git commit` will run ruff,
yamllint, ansible-lint, and the unit tests against the diff. To run the
full suite manually:

```bash
pre-commit run --all-files
```

## Running tests

```bash
# Unit tests (fast, no external services).
pytest tests/unit/ -v

# Scripted-LLM eval (orchestrator end-to-end against synthetic fixtures).
pytest tests/eval/ -v

# Localhost integration smoke (uses the stub LLM in tests/integration/).
python tests/integration/stub_llm.py 8765 &
ansible-playbook tests/integration/playbook_localhost_smoke.yml \
  -e provider=claude -e endpoint=http://127.0.0.1:8765 \
  -e api_key=stub -e model=stub-claude
```

CI runs all three on every PR (`.github/workflows/ci.yml`).

## Branching + PR conventions

- `main` is branch-protected. All changes go through a PR — no direct
  commits.
- Use a topic branch named for the change (`fix/...`, `feat/...`,
  `harden/...`, `docs/...`, `examples/...`, `chore/release-X.Y.Z`).
- Single-line commit subjects. The first line ≤ 72 chars; no trailing
  body unless you genuinely need it (commits should not duplicate the PR
  description).
- One PR = one logical change. Don't bundle unrelated cleanups.

## Changelog

Every user-visible change needs a changelog fragment under
`changelogs/fragments/`. The format is YAML with one or more sections:

```yaml
# changelogs/fragments/<short-name>.yml
---
security_fixes:
  - "Reject ``foo`` because reasons."
bugfixes:
  - "Stop crashing when ``bar`` is empty."
minor_changes:
  - "Add ``baz`` task arg."
breaking_changes:
  - "``qux`` arg removed; use ``quux`` instead."
```

Sections (in canonical order): `release_summary`, `major_changes`,
`minor_changes`, `breaking_changes`, `deprecated_features`,
`removed_features`, `security_fixes`, `bugfixes`, `known_issues`. Lines
stay under 160 chars (yamllint enforces this). Use ``>`` block scalars
for prose to wrap cleanly.

> **Don't use the `trivial` section.** `antsibull-changelog` accepts it
> (it's even configured in `changelogs/config.yaml`), but the
> `ansible-test sanity` changelog rule rejects it and we run sanity as a
> required check. Use `minor_changes` for small user-visible cleanups,
> or skip the fragment entirely for repo-internal-only churn (CI tweaks
> the user will never see).

Fragments are consumed by `antsibull-changelog` at release time and
folded into `changelogs/changelog.yaml`. **Don't** edit
`changelogs/changelog.yaml` directly — your fragment is the source of
truth.

## Adding an examples/ domain

See [`examples/README.md`](examples/README.md#adding-a-domain). TL;DR:
new folder + `rules.yml` + `README.md` + ≥1 scenario playbook + index
table row + `ansible-lint examples/<domain>/`.

## Coding standards

- **Python**: ruff handles formatting + lint. `target-version = "py310"`,
  `line-length = 110`, `from __future__ import annotations` everywhere
  for `X | Y` annotations.
- **Comments**: only when the *why* is non-obvious. Don't restate what
  the code does. Don't reference tasks/issues — those rot.
- **Defensive code**: validate at boundaries (LLM responses, user task
  args, file I/O). Trust internal call sites.
- **Security boundary**: the rule layer + sandbox is the trust boundary.
  Anything inside `plugins/module_utils/sandbox.py` and
  `plugins/module_utils/rules.py` warrants extra scrutiny — adversarial
  tests required when changing AST validation.

## Reporting bugs

Open a GitHub issue with: ansible-core version, Python version, provider
+ model, the playbook task that reproduces, and the error / unexpected
output. If it involves a destructive misbehavior, treat it as security
and follow [SECURITY.md](SECURITY.md) instead.

## Reporting security issues

See [SECURITY.md](SECURITY.md). **Do not** open a public issue for a
sandbox escape, rule-layer bypass, or anything that could be used to run
unintended commands on a target host.
