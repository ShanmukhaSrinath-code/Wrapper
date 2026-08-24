# promtail — log shipper

| | |
|---|---|
| Image | `grafana/promtail` |
| Port | none |
| Reads | the Docker socket + container logs |
| Writes to | loki |
| Config | `promtail-config.yaml` |

It tails container stdout and forwards to Loki. This is why every process must
emit JSON on stdout and nothing else — the one exception, Celery's startup
banner, is suppressed with `--without-banner`.
