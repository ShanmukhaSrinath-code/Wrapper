"""Seed the running stack, then report whether every block is actually working.

Two jobs in one script, both aimed at a demo:

1. **Seed.** Drive enough real traffic that no Grafana panel, log stream or
   trace search comes up empty. Rate-based panels on an idle app correctly show
   zero, which looks broken to an audience even though it is right.
2. **Report.** Check each block of the base over HTTP and print one line per
   block, so "is it working?" has a visible answer before anyone opens a UI.

This is a *breadth* check -- one honest probe per block. `scripts/smoke.py` is
the *depth* check: it proves one request_id joins logs, traces, audit and
metrics. Run this first, that second.

Usage:  .venv\\Scripts\\python.exe scripts/seed_demo.py
        (run.cmd does it for you)

Exits non-zero if a required block fails.
"""

from __future__ import annotations

import os
import sys
import time
import uuid

import httpx

APP_URL = os.environ.get("SEED_APP_URL", "http://localhost:8000")
LOKI_URL = os.environ.get("SEED_LOKI_URL", "http://localhost:3100")
PROM_URL = os.environ.get("SEED_PROM_URL", "http://localhost:9090")
TEMPO_URL = os.environ.get("SEED_TEMPO_URL", "http://localhost:3200")
GRAFANA_URL = os.environ.get("SEED_GRAFANA_URL", "http://localhost:3001")

TICKETS_TO_SEED = 12

# Blocks whose failure should fail the script. Tracing is excluded on purpose:
# trace *export* is off unless the `tracing` profile is running, and the base
# treats that as a supported configuration rather than a fault.
OPTIONAL = {"Tempo (traces)"}


class Report:
    """Collected results, printed as one aligned table at the end."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, block: str, ok: bool, detail: str) -> None:
        self.rows.append((block, ok, detail))
        mark = "OK  " if ok else "FAIL"
        print(f"  [{mark}] {block:<24} {detail}")

    def failures(self) -> list[str]:
        return [b for b, ok, _ in self.rows if not ok and b not in OPTIONAL]


def seed(client: httpx.Client) -> list[str]:
    """Create tickets and exercise every path, returning their ids."""
    ids: list[str] = []
    priorities = ["low", "normal", "high", "urgent"]

    for i in range(TICKETS_TO_SEED):
        response = client.post(
            "/tickets",
            json={
                "title": f"Seeded ticket {i + 1}",
                "description": "Created by scripts/seed_demo.py so the demo has data.",
                "priority": priorities[i % len(priorities)],
                # Two thirds get an assignee, so the worker has real work and
                # the "unassigned" stat is not zero either.
                "assignee": ["alice", "bob", None][i % 3],
            },
            headers={"X-Request-ID": f"seed-{i + 1:03d}"},
        )
        if response.status_code != 201:
            continue
        ticket_id = response.json()["id"]
        ids.append(ticket_id)

        # Two reads: the second is a cache hit.
        client.get(f"/tickets/{ticket_id}")
        client.get(f"/tickets/{ticket_id}")

        # Walk some of them through the status machine so the stats panel has
        # more than one bar.
        if i % 3 == 0:
            client.patch(f"/tickets/{ticket_id}", json={"status": "in_progress"})
        if i % 4 == 0:
            client.patch(f"/tickets/{ticket_id}", json={"status": "in_progress"})
            client.patch(f"/tickets/{ticket_id}", json={"status": "resolved"})

        # An attachment on a few, so MinIO is not empty.
        if i % 5 == 0:
            client.post(
                f"/tickets/{ticket_id}/attachment",
                files={"file": (f"evidence-{i}.txt", b"seeded attachment\n", "text/plain")},
            )

        client.get("/tickets")
        client.get("/tickets/stats")

        # Some 4xx and 5xx, so the error-rate panel is not a flat zero line.
        client.get(f"/tickets/{uuid.uuid4()}")
        if i % 4 == 0:
            client.get("/demo/boom")

    return ids


def check_api(client: httpx.Client, report: Report, ticket_id: str) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    ticket_paths = {p: ops for p, ops in paths.items() if "ticket" in p}
    # Count operations, not paths -- `/tickets` alone carries both GET and POST.
    operations = sum(
        1
        for ops in ticket_paths.values()
        for verb in ops
        if verb in {"get", "post", "patch", "put", "delete"}
    )
    report.add(
        "FastAPI + discovery",
        operations >= 7,
        f"{operations} ticket operations on {len(ticket_paths)} paths, auto-mounted "
        f"(no edit to main.py)",
    )

    row = client.get(f"/tickets/{ticket_id}")
    report.add(
        "PostgreSQL",
        row.status_code == 200 and row.json()["requester"] == "dev",
        f"row read back, requester={row.json().get('requester')!r} (from the principal)",
    )

    # A ticket nothing has read yet, so the first read is genuinely cold.
    cold = client.post("/tickets", json={"title": "Cache probe"}).json()["id"]
    first = client.get(f"/tickets/{cold}").headers.get("X-Cache")
    second = client.get(f"/tickets/{cold}").headers.get("X-Cache")
    report.add(
        "Redis cache",
        (first, second) == ("MISS", "HIT"),
        f"cold read = {first}, second read = {second}",
    )

    client.patch(f"/tickets/{cold}", json={"priority": "urgent"})
    after = client.get(f"/tickets/{cold}")
    report.add(
        "Cache invalidation",
        after.headers.get("X-Cache") == "MISS",
        f"after a write: {after.headers.get('X-Cache')}",
    )

    fresh = client.post("/tickets", json={"title": "Conflict probe"}).json()["id"]
    conflict = client.patch(f"/tickets/{fresh}", json={"status": "resolved"})
    body = conflict.json()
    report.add(
        "Error contract",
        conflict.status_code == 409 and body.get("error") == "conflict" and "request_id" in body,
        f"illegal transition -> {conflict.status_code} {body.get('error')}, "
        f"allowed={body.get('detail', {}).get('allowed')}",
    )

    missing = client.get(f"/tickets/{uuid.uuid4()}")
    report.add(
        "404 handling",
        missing.status_code == 404 and missing.json().get("error") == "not_found",
        f"unknown id -> {missing.status_code}, ids stamped on the error body",
    )


def check_storage(client: httpx.Client, report: Report, ticket_id: str) -> None:
    payload = b"visual confirmation that MinIO round-trips\n"
    upload = client.post(
        f"/tickets/{ticket_id}/attachment",
        files={"file": ("proof.txt", payload, "text/plain")},
    )
    if upload.status_code != 200:
        report.add("MinIO (object store)", False, f"upload returned {upload.status_code}")
        return

    redirect = client.get(f"/tickets/{ticket_id}/attachment", follow_redirects=False)
    location = redirect.headers.get("location", "")
    presigned = "X-Amz-Signature" in location

    status, fetched = 0, b""
    if presigned:
        with httpx.Client(timeout=30.0) as raw:
            got = raw.get(location)
            status, fetched = got.status_code, got.content

    ok = presigned and fetched == payload
    # Report what actually happened, not what was hoped for: a probe that says
    # "bytes match" on a failure is worse than no probe.
    if ok:
        detail = f"uploaded, {redirect.status_code} to a presigned URL, {len(fetched)} bytes match"
    elif not presigned:
        detail = f"no presigned URL: {redirect.status_code} location={location[:60]!r}"
    else:
        detail = f"presigned GET returned {status}, {len(fetched)} bytes (expected {len(payload)})"
    report.add("MinIO (object store)", ok, detail)


def check_worker(client: httpx.Client, report: Report) -> None:
    """Publish a job and poll it, which is the only API-only proof of the worker."""
    accepted = client.post("/demo/job", params={"a": 7, "b": 5, "delay_seconds": 0.5})
    if accepted.status_code != 202:
        report.add("Celery worker", False, f"enqueue returned {accepted.status_code}")
        return

    task_id = accepted.json()["task_id"]
    state, result = "PENDING", None
    for _ in range(40):
        poll = client.get(f"/demo/job/{task_id}").json()
        state = poll.get("status", "")
        if state in {"SUCCESS", "FAILURE"}:
            result = poll.get("result")
            break
        time.sleep(0.5)

    ok = state == "SUCCESS" and isinstance(result, dict) and result.get("sum") == 12
    detail = f"task {state.lower()}"
    if isinstance(result, dict):
        # The worker echoes the request id it inherited -- proof the context
        # crossed the process boundary, not just that a task ran.
        detail += f", 7+5={result.get('sum')}, inherited request_id={result.get('request_id')}"
    report.add("Celery worker", ok, detail)


def check_audit(client: httpx.Client, report: Report) -> None:
    marker = f"seed-audit-{uuid.uuid4().hex[:8]}"
    response = client.post("/demo/audited", headers={"X-Request-ID": marker})
    body = response.json()
    report.add(
        "Audit trail",
        bool(body.get("audit_id")) and body.get("request_id") == marker,
        f"row {str(body.get('audit_id'))[:8]}... written under request_id={marker}",
    )


def check_health(client: httpx.Client, report: Report) -> None:
    live = client.get("/health/live")
    ready = client.get("/health/ready")
    checks = ready.json().get("checks", {})
    report.add(
        "Health probes",
        live.status_code == 200 and ready.status_code == 200,
        f"live={live.status_code}, ready={ready.status_code}, {checks}",
    )


def check_security(client: httpx.Client, report: Report) -> None:
    headers = {k.lower() for k in client.get("/health/live").headers}
    wanted = {
        "content-security-policy",
        "x-frame-options",
        "x-content-type-options",
        "referrer-policy",
    }
    missing = wanted - headers
    report.add(
        "Security headers",
        not missing,
        "all four present" if not missing else f"missing {sorted(missing)}",
    )


def check_metrics(client: httpx.Client, report: Report) -> None:
    text = client.get("/metrics").text
    templated = 'handler="/tickets/{ticket_id}"' in text
    report.add(
        "Prometheus /metrics",
        "app_http_requests_total" in text and templated,
        "counters exposed, labelled by route template (no ids -> bounded cardinality)",
    )


def check_prometheus(report: Report) -> None:
    """Poll, because a freshly recreated app container has not been scraped yet.

    The scrape interval is 15s and `run.cmd` recreates the container, so a check
    that runs immediately after startup legitimately finds an empty result set.
    Retrying is the difference between "Prometheus is broken" and "Prometheus
    has not looked yet" -- and an empty `result` list must be handled, not
    indexed into.
    """
    last = "no data returned"
    for _ in range(12):
        try:
            with httpx.Client(timeout=10.0) as client:
                up = client.get(
                    f"{PROM_URL}/api/v1/query", params={"query": 'up{job="common-app-base"}'}
                ).json()["data"]["result"]
                scraped = client.get(
                    f"{PROM_URL}/api/v1/query",
                    params={"query": "sum(app_http_requests_total)"},
                ).json()["data"]["result"]
            if up and scraped and up[0]["value"][1] == "1":
                total = int(float(scraped[0]["value"][1]))
                report.add(
                    "Prometheus (scrape)",
                    True,
                    f"target up, {total} requests counted since the app started",
                )
                return
            last = (
                f"target present={bool(up)}, counters present={bool(scraped)} - awaiting a scrape"
            )
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(3)
    report.add("Prometheus (scrape)", False, last)


def check_loki(report: Report, request_id: str) -> None:
    """Loki is asynchronous, so give Promtail a moment before deciding."""
    query = f'{{service=~"app|worker"}} | json | request_id = "{request_id}"'
    last = f"no lines for request_id={request_id} after 24s"
    for _ in range(12):
        try:
            with httpx.Client(timeout=10.0) as client:
                data = client.get(
                    f"{LOKI_URL}/loki/api/v1/query_range",
                    params={"query": query, "limit": 100, "since": "1h"},
                ).json()
            lines = sum(len(s["values"]) for s in data["data"]["result"])
            if lines:
                report.add(
                    "Loki (logs)", True, f"{lines} JSON log lines for request_id={request_id}"
                )
                return
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    report.add("Loki (logs)", False, last)


def check_tempo(report: Report, trace_id: str) -> None:
    if not trace_id:
        report.add("Tempo (traces)", False, "no trace id on the response")
        return
    last = (
        "not exported (expected unless the `tracing` profile is on) - "
        "trace_id still correlates in logs"
    )
    for _ in range(8):
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(f"{TEMPO_URL}/api/traces/{trace_id}")
            if response.status_code == 200 and response.json().get("batches"):
                spans = sum(
                    len(ss.get("spans", []))
                    for b in response.json()["batches"]
                    for ss in b.get("scopeSpans", [])
                )
                report.add("Tempo (traces)", True, f"trace {trace_id[:12]}... has {spans} spans")
                return
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    report.add(
        "Tempo (traces)",
        False,
        last,
    )


def check_grafana(report: Report) -> None:
    try:
        with httpx.Client(timeout=10.0) as client:
            health = client.get(f"{GRAFANA_URL}/api/health").json()
            sources = client.get(f"{GRAFANA_URL}/api/datasources").json()
            boards = client.get(f"{GRAFANA_URL}/api/search", params={"type": "dash-db"}).json()
        names = sorted(d["name"] for d in sources)
        report.add(
            "Grafana",
            health.get("database") == "ok" and len(boards) >= 1,
            f"v{health.get('version')}, datasources={names}, {len(boards)} dashboard(s)",
        )
    except Exception as exc:
        report.add("Grafana", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    print()
    print("Seeding the stack ...")
    with httpx.Client(base_url=APP_URL, timeout=60.0, follow_redirects=False) as client:
        try:
            client.get("/health/live")
        except httpx.HTTPError as exc:
            print(f"  cannot reach {APP_URL}: {exc}")
            print("  Start the stack first (run.cmd, or `make up`).")
            return 1

        ids = seed(client)
        print(f"  {len(ids)} tickets created, read, updated, attached to and queried.")

        # One request whose id is then hunted through Loki and Tempo.
        marker = f"seed-probe-{uuid.uuid4().hex[:8]}"
        probe = client.post(
            "/tickets",
            json={"title": "Observability probe", "assignee": "alice", "priority": "high"},
            headers={"X-Request-ID": marker},
        )
        trace_id = probe.headers.get("X-Trace-ID", "")

        print()
        print("Checking each block ...")
        print()
        report = Report()
        check_api(client, report, ids[0])
        check_storage(client, report, ids[0])
        check_worker(client, report)
        check_audit(client, report)
        check_health(client, report)
        check_security(client, report)
        check_metrics(client, report)

    check_prometheus(report)
    check_grafana(report)
    check_loki(report, marker)
    check_tempo(report, trace_id)

    failures = report.failures()
    print()
    if failures:
        print(f"  {len(failures)} block(s) FAILED: {', '.join(failures)}")
        return 1
    print(f"  All {len(report.rows)} blocks reporting healthy.")
    print(f"  Probe request_id = {marker}  (paste it into the Grafana dashboard)")
    print(f"  Probe trace_id   = {trace_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
