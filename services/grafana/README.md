# grafana — dashboards

| | |
|---|---|
| Image | `grafana/grafana` |
| Port | 3001 on the host (3000 inside) — 3000 is often taken |
| Reads from | prometheus, loki, tempo |
| Config | `provisioning/` (datasources + dashboard providers), `dashboards/` |

Everything is provisioned from files, so a fresh stack comes up with the
datasources and the dashboard already in place — nothing to click.

Override the host port with `GRAFANA_PORT` if 3001 is also taken.
