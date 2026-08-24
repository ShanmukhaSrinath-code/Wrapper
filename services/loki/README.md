# loki — log aggregation

| | |
|---|---|
| Image | `grafana/loki` |
| Port | 3100 |
| Fed by | promtail |
| Read by | grafana |
| Config | `loki-config.yaml` |

Every application log line is JSON containing `request_id` and `trace_id`, so
one request's lines are found with:

```
{service="app"} | json | request_id = "<id>"
```
