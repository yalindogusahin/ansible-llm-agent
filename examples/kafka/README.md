# Kafka playbooks

Investigation playbooks for Kafka brokers. Assumptions:

- Brokers run as a systemd-managed service named `kafka` (adjust prompt if
  yours is `confluent-kafka` or similar).
- Default listener on port 9092 (PLAINTEXT). For TLS / SASL deployments,
  the playbooks still work but the agent may need extra context in the prompt.
- `kafka-topics.sh`, `kafka-consumer-groups.sh`, and `kafka-broker-api-versions.sh`
  are on PATH (typically `/opt/kafka/bin/` or `/usr/bin/` on packaged installs).
- Logs live under `/var/log/kafka/` (overridable via `log4j.properties`).

`rules.yml` allows the Kafka admin scripts (`kafka-topics`, `kafka-consumer-groups`,
`kafka-broker-api-versions`) read-only — these are the diagnostic-only entry
points, not the destructive ones (`kafka-delete-records` is **not** allowed).

## Playbooks

- **broker-down.yml** — port 9092 not listening, or process gone. Walks
  `ss -tlnp`, `ps aux | grep kafka`, `systemd` journal for the unit.
- **consumer-lag.yml** — a consumer group is falling behind. Pulls
  `kafka-consumer-groups --describe`, identifies high-lag partitions, checks
  broker health.
