# Releasing

How releases are cut, tagged, and published to Ansible Galaxy.

## Versioning policy

This collection follows [Semantic Versioning](https://semver.org/):

- **MAJOR** (1.0 onwards): incompatible API changes. Includes any
  removal/rename of a task arg, a return-shape change, a tightening of
  the rule layer that rejects previously-accepted snippets, or a
  default-value flip a user might depend on.
- **MINOR**: backwards-compatible feature additions, new providers, new
  task args with safe defaults, expanded `examples/`.
- **PATCH**: bug fixes and security fixes that don't change the API.

**Pre-1.0 (0.x) caveat.** While the collection is in `0.x`, minor bumps
*may* contain breaking changes — that's expected for pre-1.0 software.
The 0.3.0 refactor (shell-first tool-use loop) and the 0.3.4 sandbox
tightening (categorical `os.system` rejection) are examples. The 1.0
release locks the public surface; after that, breaking changes require
a major bump and a deprecation window.

## Deprecation policy (1.0 onwards)

When deprecating a task arg or behavior:

1. Mark it deprecated in the next minor release (changelog
   `deprecated_features`, runtime warning when used).
2. Keep it functional for at least **two minor releases**.
3. Remove in the next major (changelog `removed_features`, with
   migration guidance in the release summary).

## Support matrix

| Component | Supported |
|---|---|
| `ansible-core` | ≥ 2.15 (declared in `meta/runtime.yml`) |
| Python (controller + module) | 3.10, 3.11, 3.12 (CI tests all three) |
| Controller OS | Linux (Ubuntu 22.04+, RHEL 9+); macOS works with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` workaround documented in README |
| Target OS | Any Linux with `bwrap`, `firejail`, `nsjail`, or just rlimit fallback |
| Providers | Claude (Anthropic), OpenAI / OpenAI-compatible (vLLM), AWS Bedrock, Ollama |

Dropping support for any row is a major-bump change.

## Release flow

Releases are PR-driven. The release PR bumps the version and folds the
changelog fragments; merging the PR and pushing the matching tag
triggers `release.yml` which builds and publishes to Galaxy.

### 1. Open the release PR

```bash
git checkout -b release/X.Y.Z
```

Edit `galaxy.yml`:

```yaml
version: X.Y.Z
```

Add a release entry to `changelogs/changelog.yaml`. Pick the section
labels that match the fragments under `changelogs/fragments/`. Include a
`release_summary` if the release has a theme worth calling out (a
security release, a refactor, a beta milestone). Then delete the
fragments you've folded in:

```bash
rm changelogs/fragments/*.yml
```

Commit:

```bash
git add galaxy.yml changelogs/changelog.yaml changelogs/fragments/
git commit -m "chore: release X.Y.Z"
git push -u origin release/X.Y.Z
gh pr create --title "chore: release X.Y.Z" --body "..."
```

Wait for CI green, then merge.

### 2. Tag the release

The release workflow only fires on `v*` tag pushes. The tag must equal
`v` + `galaxy.yml` version (the workflow asserts this).

```bash
git checkout main
git pull --ff-only
git tag vX.Y.Z
git push origin vX.Y.Z
```

### 3. The release workflow does the rest

`.github/workflows/release.yml` will:

1. Verify the tag matches `galaxy.yml`.
2. `ansible-galaxy collection build .`
3. `ansible-galaxy collection publish ./*.tar.gz` using the
   `ANSIBLE_GALAXY_TOKEN` repo secret.
4. Create a GitHub Release with the built `.tar.gz` attached and
   auto-generated release notes.

Verify on https://galaxy.ansible.com/ui/repo/published/yalindogusahin/ansible_ai/
that the new version is listed.

## Required repo secrets

| Secret | Purpose |
|---|---|
| `ANSIBLE_GALAXY_TOKEN` | API token for `galaxy.ansible.com` publish. Create at https://galaxy.ansible.com/me/preferences (Settings → API token). The release workflow fails fast if this is unset. |

## Hotfix / security release flow

Same as a normal release, just on a shorter loop:

1. PR with the fix + a `security_fixes` fragment.
2. Merge to main, cut a patch (or minor) bump.
3. Tag and push. Galaxy publish.
4. If the fix is for a coordinated disclosure, also publish a
   [GitHub Security Advisory](https://github.com/yalindogusahin/ansible-ai-agent/security/advisories)
   pointing at the fix commit and the affected version range.

## Yanking a release

Galaxy doesn't support yank — once a version is published, it's
immutable. If a release ships broken, publish a follow-up patch
immediately and add a `known_issues` fragment to the next release noting
which version to skip.
