# Redis playbooks

Investigation playbooks for Redis instances. Assumptions:

- Redis runs as systemd unit `redis` (Debian/Ubuntu) or `redis-server` /
  `redis_6379` (RHEL / multi-instance). Adjust the prompts if your unit
  name differs.
- `redis-cli` on PATH and connectable from the playbook user. If the
  instance requires `AUTH`, pass it via `-e redis_auth=<pwd>` or wire a
  `REDISCLI_AUTH` env var on the target. The playbooks use `redis-cli`
  with safe read-only commands (`INFO`, `SLOWLOG`, `MEMORY USAGE`,
  `CLIENT LIST`, `CLUSTER INFO`).
- Logs at `/var/log/redis/` (default Debian/Ubuntu location).

`rules.yml` allows `redis-cli` read-only patterns. **`FLUSHDB`, `FLUSHALL`,
`SHUTDOWN`, `CONFIG SET` cannot be denied at the binary level** — they're
all subcommands of `redis-cli`. Safety here comes from the prompts only
asking for diagnostic commands. If you need hard guarantees, point Redis
at a read-only replica (`replicaof`) for these investigations.

## Playbooks

- **memory-pressure.yml** — `INFO memory` shows the instance close to
  `maxmemory` or evicting heavily. Identifies the largest keys / data
  patterns.
- **slow-commands.yml** — clients reporting timeouts. Pulls `SLOWLOG GET`
  and correlates with current client list and command stats.
