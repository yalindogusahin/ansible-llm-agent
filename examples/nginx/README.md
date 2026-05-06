# nginx playbooks

Investigation playbooks for nginx web servers / reverse proxies. Assumptions:

- nginx runs as systemd unit `nginx`. Adjust journalctl unit name if yours
  differs (`openresty`, `nginx-stable`, etc).
- Default access/error logs at `/var/log/nginx/access.log` and `error.log`.
  Custom log paths under `/var/log/nginx/**` are also covered.
- Config root at `/etc/nginx/` (`nginx.conf` + `conf.d/` + `sites-enabled/`).
- `nginx -t` (config syntax check) is allowed read-only — it doesn't touch
  state.

`rules.yml` permits `nginx -t`, `curl` (for upstream health probes), plus
reads under `/var/log/nginx/**` and `/etc/nginx/**`. **`nginx -s reload`
and `nginx -s stop` are not allowed** — these are state-changing.

## Playbooks

- **5xx-surge.yml** — error rate spiking. Walks recent error.log, identifies
  upstream failures vs config issues vs rate-limited clients.
- **config-validation.yml** — a config change is suspected to have broken
  something. Runs `nginx -t`, diffs sites-enabled state, traces include
  chain.
