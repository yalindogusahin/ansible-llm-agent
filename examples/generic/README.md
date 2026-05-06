# Generic host-level playbooks

Cross-cutting symptoms that show up on any Linux host regardless of stack:
disk pressure, OOM kills, failed systemd units. These rely only on the
collection's default allow list (no domain-specific binaries needed).

`rules.yml` here is intentionally a near-empty additive layer — it exists
mostly as a placeholder so each example follows the same pattern. The real
allow list lives in [`roles/ai_agent/defaults/main.yml`](../../roles/ai_agent/defaults/main.yml).

## Playbooks

- **disk-pressure.yml** — `df` + `du` walk to find the largest consumers under
  the most-utilised mount. Useful when an alert fires for `/var` or `/`.
- **oom-kill.yml** — pulls recent OOM-kill records from `dmesg` / `journalctl`
  and correlates with current `ps` memory hogs.
- **systemd-failed.yml** — finds units in `failed` state and reads each one's
  recent journal lines to diagnose the cause.
