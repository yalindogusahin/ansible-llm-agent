# PostgreSQL playbooks

Investigation playbooks for a PostgreSQL server. Assumptions:

- Server runs as systemd unit `postgresql` (Debian/Ubuntu) or `postgresql-XX`
  (RHEL family). Adjust the prompt's `journalctl -u <unit>` if yours differs.
- `psql` on PATH and the agent's user can connect (typically peer auth as
  `postgres`, or a `~/.pgpass` for the playbook user).
- Logs live under `/var/log/postgresql/` (Debian) or `/var/lib/pgsql/data/log/`
  (RHEL). Both paths are in `rules.yml`.

`rules.yml` allows `psql -c <SELECT-only>`, `pg_isready`, and reads under the
common log/config dirs. **`psql -c` cannot be syntactically restricted to
SELECT** — the rule layer permits the binary, and the safety comes from the
prompt asking the agent to run only catalog/`pg_stat_*` queries. If you need
hard guarantees, point the playbook at a read-replica or a role with
`SELECT`-only grants.

## Playbooks

- **connection-issues.yml** — clients getting `connection refused` or
  `sorry, too many clients already`. Checks listener, max_connections, current
  pool usage.
- **slow-queries.yml** — pulls `pg_stat_activity` for currently running long
  queries plus lock waits.
