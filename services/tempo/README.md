# tempo — distributed tracing

| | |
|---|---|
| Image | `grafana/tempo` |
| Ports | 3200 (query API), 4318 (OTLP/HTTP ingest) |
| Fed by | app, worker (OpenTelemetry) |
| Read by | grafana |
| Config | `tempo-config.yaml` |

**Opt-in.** Tempo runs under the `tracing` profile, because the default stack is
already nine services:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318   docker compose -f deploy/docker-compose.yml --profile tracing up -d
```

Without it, spans are still generated — so `trace_id` on a log line or an error
response is real — they are simply not stored.
