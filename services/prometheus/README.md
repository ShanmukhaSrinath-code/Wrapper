# prometheus — metrics collection

| | |
|---|---|
| Image | `prom/prometheus` |
| Port | 9090 |
| Scrapes | app (`/metrics`), loki, itself |
| Config | `prometheus.yml` |

Metrics are labelled by **route template, method and status only**. Correlation
ids are deliberately not labels: they are unbounded and would destroy
cardinality. To join a metric to a request, go via the logs — see
`../../ARCHITECTURE.md`.
