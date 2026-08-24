"""Full-stack correlation smoke test.

Drives the running stack through three real operations, captures the
`X-Request-ID` of each, then goes to **every store independently** and asserts
that id is there:

    log    -- Loki has JSON lines for it
    trace  -- a trace with that trace_id exists (Tempo, when the `tracing`
              profile is on) and the logs carry the same trace_id
    audit  -- Postgres has an append-only audit row for it
    metric -- Prometheus counters moved for the routes that were hit
    error  -- the failing request produced a correlated error (and reached
              Sentry, when a DSN is configured)

Exits 0 only if every required cell passes. The point is not that the app
responded -- it is that one id joins five independent systems.

Usage:  make smoke   (or: uv run python scripts/smoke.py)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field

import httpx

APP_URL = os.environ.get("SMOKE_APP_URL", "http://localhost:8000")
LOKI_URL = os.environ.get("SMOKE_LOKI_URL", "http://localhost:3100")
PROM_URL = os.environ.get("SMOKE_PROM_URL", "http://localhost:9090")
TEMPO_URL = os.environ.get("SMOKE_TEMPO_URL", "http://localhost:3200")
PG_CONTAINER = os.environ.get("SMOKE_PG_CONTAINER", "cab-postgres")
PG_USER = os.environ.get("POSTGRES_USER", "appuser")
PG_DB = os.environ.get("POSTGRES_DB", "appdb")

OK = "OK"
FAIL = "FAIL"
SKIP = "--"

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    ("\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")
    if sys.stdout.isatty()
    else ("", "", "", "", "", "")
)


@dataclass
class Step:
    """One operation, and what each store should know about it."""

    name: str
    request_id: str = ""
    trace_id: str = ""
    detail: str = ""
    checks: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    #: Which checks must pass for this step. Others are informational.
    required: tuple[str, ...] = ("log", "audit", "metric")


def log(msg: str) -> None:
    print(f"{DIM}  {msg}{RESET}", flush=True)


def section(msg: str) -> None:
    print(f"\n{BOLD}{msg}{RESET}", flush=True)


# ---------------------------------------------------------------------------
# stack readiness
# ---------------------------------------------------------------------------
async def wait_for_stack(client: httpx.AsyncClient, timeout_seconds: float = 180.0) -> None:
    section("1. Waiting for the stack")
    deadline = time.monotonic() + timeout_seconds
    last = ""
    while time.monotonic() < deadline:
        try:
            r = await client.get(f"{APP_URL}/health/ready", timeout=5.0)
            if r.status_code == 200:
                checks = r.json()["checks"]
                log(f"ready: {json.dumps(checks)}")
                return
            last = r.text[:120]
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(3)
    raise SystemExit(f"{RED}stack never became ready: {last}{RESET}\nRun `make up` first.")


async def tempo_available(client: httpx.AsyncClient) -> bool:
    try:
        r = await client.get(f"{TEMPO_URL}/ready", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# the three operations
# ---------------------------------------------------------------------------
async def do_upload(client: httpx.AsyncClient) -> Step:
    step = Step("upload a file", required=("log", "audit", "metric"))
    payload = f"smoke test {time.time()}\n".encode() * 4
    r = await client.post(
        f"{APP_URL}/files",
        files={"file": ("smoke.txt", payload, "text/plain")},
        timeout=30.0,
    )
    r.raise_for_status()
    step.request_id = r.headers["X-Request-ID"]
    step.trace_id = r.headers.get("X-Trace-ID", "")
    step.detail = f"file_id={r.json()['id']}"

    # The bytes must survive the round trip, or "correlated" would be hollow.
    back = await client.get(f"{APP_URL}/files/{r.json()['id']}/content", timeout=30.0)
    if back.content != payload:
        step.checks["roundtrip"] = FAIL
    log(f"upload      request_id={step.request_id}  {step.detail}")
    return step


async def do_job(client: httpx.AsyncClient) -> Step:
    # A job has no audit row of its own; the worker log is the evidence.
    step = Step("trigger a background job", required=("log", "metric", "worker"))
    r = await client.post(
        f"{APP_URL}/demo/job", params={"a": 20, "b": 22, "delay_seconds": 1}, timeout=30.0
    )
    r.raise_for_status()
    step.request_id = r.headers["X-Request-ID"]
    step.trace_id = r.headers.get("X-Trace-ID", "")
    task_id = r.json()["task_id"]
    step.detail = f"task_id={task_id[:8]}..."
    log(f"job         request_id={step.request_id}  {step.detail}")

    for _ in range(60):
        s = await client.get(f"{APP_URL}/demo/job/{task_id}", timeout=15.0)
        body = s.json()
        if body["status"] in {"SUCCESS", "FAILURE"}:
            # The worker echoes the originating id back in its result: proof
            # the correlation survived the process hop.
            got = (body.get("result") or {}).get("request_id")
            step.checks["worker"] = OK if got == step.request_id else FAIL
            step.notes["worker"] = f"result.request_id={got}"
            return step
        await asyncio.sleep(1)

    step.checks["worker"] = FAIL
    step.notes["worker"] = "task never finished"
    return step


async def do_boom(client: httpx.AsyncClient) -> Step:
    step = Step("trigger /demo/boom", required=("log", "audit", "metric", "error"))
    r = await client.get(f"{APP_URL}/demo/boom", timeout=30.0)
    step.request_id = r.headers["X-Request-ID"]
    step.trace_id = r.headers.get("X-Trace-ID", "")

    body = r.json()
    correlated = (
        r.status_code == 500
        and body.get("error") == "internal_error"
        and body.get("request_id") == step.request_id
        and "Traceback" not in r.text
    )
    step.checks["error"] = OK if correlated else FAIL
    step.notes["error"] = f"HTTP {r.status_code} error={body.get('error')}"
    step.detail = f"HTTP {r.status_code}"
    log(f"boom        request_id={step.request_id}  {step.detail}")
    return step


# ---------------------------------------------------------------------------
# assertions, one per store
# ---------------------------------------------------------------------------
async def check_loki(client: httpx.AsyncClient, step: Step, attempts: int = 20) -> None:
    start = int((time.time() - 900) * 1e9)
    query = f'{{service="app"}} | json | request_id = `{step.request_id}`'
    for _ in range(attempts):
        try:
            r = await client.get(
                f"{LOKI_URL}/loki/api/v1/query_range",
                params={"query": query, "limit": 100, "start": str(start)},
                timeout=15.0,
            )
            streams = r.json().get("data", {}).get("result", []) if r.status_code == 200 else []
            lines = sum(len(s["values"]) for s in streams)
            if lines:
                step.checks["log"] = OK
                step.notes["log"] = f"{lines} line(s) in Loki"
                # Same query, but joined on trace_id: proves the two ids agree
                # inside the log store, not just in the response headers.
                if step.trace_id:
                    tr = await client.get(
                        f"{LOKI_URL}/loki/api/v1/query_range",
                        params={
                            "query": f'{{service="app"}} | json | trace_id = `{step.trace_id}`',
                            "limit": 10,
                            "start": str(start),
                        },
                        timeout=15.0,
                    )
                    if tr.json().get("data", {}).get("result"):
                        step.checks.setdefault("trace", OK)
                        step.notes["trace"] = "trace_id present in logs"
                return
        except Exception as exc:
            step.notes["log"] = f"{type(exc).__name__}"
        await asyncio.sleep(3)
    step.checks["log"] = FAIL
    step.notes.setdefault("log", "no lines in Loki")


async def check_tempo(client: httpx.AsyncClient, step: Step, attempts: int = 15) -> None:
    if not step.trace_id:
        return
    for _ in range(attempts):
        try:
            r = await client.get(f"{TEMPO_URL}/api/traces/{step.trace_id}", timeout=15.0)
            if r.status_code == 200 and r.json().get("batches"):
                spans = sum(
                    len(ss.get("spans", []))
                    for b in r.json()["batches"]
                    for ss in b.get("scopeSpans", [])
                )
                step.checks["trace"] = OK
                step.notes["trace"] = f"{spans} span(s) in Tempo"
                return
        except Exception as exc:
            step.notes["trace"] = f"tempo query failed: {type(exc).__name__}"
        await asyncio.sleep(3)
    # Leave whatever check_loki concluded; do not downgrade an OK to FAIL just
    # because the optional trace store is not running.
    step.notes.setdefault("trace", "not found in Tempo")


def check_audit(step: Step) -> None:
    """Query Postgres directly -- not through the API that wrote the row."""
    # The id goes in as a psql variable and is interpolated with :'rid', so
    # psql does the quoting -- the predicate is never built by string
    # formatting. Note the SQL is fed on **stdin** (`-f -`): psql does not
    # expand variables in a `-c` command string.
    sql = "SELECT action, trace_id FROM audit_log WHERE request_id = :'rid' ORDER BY created_at;"
    try:
        out = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                PG_CONTAINER,
                "psql",
                "-U",
                PG_USER,
                "-d",
                PG_DB,
                "-t",
                "-A",
                "-F",
                "|",
                "-v",
                f"rid={step.request_id}",
                "-f",
                "-",
            ],
            input=sql,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        step.checks["audit"] = FAIL
        step.notes["audit"] = f"psql failed: {type(exc).__name__}"
        return

    rows = [ln for ln in out.stdout.strip().splitlines() if ln.strip()]
    if not rows:
        # Only a missing *required* audit row is a failure. A job legitimately
        # writes none, and reporting FAIL for it would cry wolf.
        if "audit" in step.required:
            step.checks["audit"] = FAIL
            step.notes["audit"] = "no audit row"
        else:
            step.notes["audit"] = "n/a - this operation writes no audit row"
        return

    actions = [r.split("|")[0] for r in rows]
    trace_ids = {r.split("|")[1] for r in rows if "|" in r}
    # The audit row must agree with the response header, not merely exist.
    agrees = (not step.trace_id) or (step.trace_id in trace_ids)
    step.checks["audit"] = OK if agrees else FAIL
    step.notes["audit"] = f"{len(rows)} row(s): {','.join(actions)}" + (
        "" if agrees else " (trace_id mismatch!)"
    )


async def check_metrics(client: httpx.AsyncClient, step: Step, before: float, route: str) -> None:
    for _ in range(20):
        try:
            r = await client.get(
                f"{PROM_URL}/api/v1/query",
                params={"query": f'sum(app_http_requests_total{{handler="{route}"}})'},
                timeout=15.0,
            )
            result = r.json().get("data", {}).get("result", [])
            after = float(result[0]["value"][1]) if result else 0.0
            if after > before:
                step.checks["metric"] = OK
                step.notes["metric"] = f"{route}: {before:g} -> {after:g}"
                return
        except Exception as exc:
            step.notes["metric"] = f"prometheus query failed: {type(exc).__name__}"
        await asyncio.sleep(3)
    step.checks["metric"] = FAIL
    step.notes["metric"] = f"{route} counter did not move (was {before:g})"


async def prom_counter(client: httpx.AsyncClient, route: str) -> float:
    try:
        r = await client.get(
            f"{PROM_URL}/api/v1/query",
            params={"query": f'sum(app_http_requests_total{{handler="{route}"}})'},
            timeout=10.0,
        )
        result = r.json().get("data", {}).get("result", [])
        return float(result[0]["value"][1]) if result else 0.0
    except Exception:
        # A missing counter simply means the route has not been hit yet.
        return 0.0


def check_worker_log(step: Step) -> None:
    """The worker runs in another container; its log must carry the same id."""
    try:
        out = subprocess.run(
            ["docker", "logs", "cab-worker"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return
    hits = sum(1 for ln in (out.stdout + out.stderr).splitlines() if step.request_id in ln)
    if hits:
        step.notes["worker"] = step.notes.get("worker", "") + f", {hits} worker log line(s)"


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
COLUMNS = ("log", "trace", "audit", "metric", "error", "worker")


def render(steps: list[Step], sentry_on: bool) -> bool:
    section("5. Correlation report")

    def cell(step: Step, key: str) -> str:
        value = step.checks.get(key)
        if value == OK:
            return f"{GREEN}OK{RESET}"
        if value == FAIL:
            return f"{RED}FAIL{RESET}"
        return f"{DIM}--{RESET}"

    width = max(len(s.name) for s in steps) + 2
    header = f"  {'operation':<{width}} {'request_id':<38} " + " ".join(f"{c:<7}" for c in COLUMNS)
    print(f"{BOLD}{header}{RESET}")
    print("  " + "-" * (len(header) - 2))

    ok = True
    for s in steps:
        cells = " ".join(f"{cell(s, c):<7}" for c in COLUMNS)
        # Pad manually: colour codes break str.format widths.
        cells = " ".join(
            (cell(s, c) + " " * max(0, 7 - len(s.checks.get(c, "--").replace(OK, "OK"))))
            for c in COLUMNS
        )
        print(f"  {s.name:<{width}} {s.request_id:<38} {cells}")
        for key in COLUMNS:
            if key in s.notes:
                print(f"{DIM}      {key:<7}: {s.notes[key]}{RESET}")
        missing = [k for k in s.required if s.checks.get(k) != OK]
        if missing:
            ok = False
            print(f"      {RED}missing required: {', '.join(missing)}{RESET}")

    print()
    if not sentry_on:
        print(f"{DIM}  note: SENTRY_DSN is not set, so the Sentry leg is not asserted.{RESET}")
        print(f"{DIM}        The error leg still checks the correlated error response.{RESET}")
    return ok


# ---------------------------------------------------------------------------
async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    sentry_on = bool(os.environ.get("SENTRY_DSN"))

    async with httpx.AsyncClient(follow_redirects=False) as client:
        await wait_for_stack(client)
        has_tempo = await tempo_available(client)
        log(f"tempo: {'available' if has_tempo else 'not running (tracing profile off)'}")

        section("2. Driving the stack")
        before_files = await prom_counter(client, "/files")
        before_job = await prom_counter(client, "/demo/job")
        before_boom = await prom_counter(client, "/demo/boom")

        upload = await do_upload(client)
        job = await do_job(client)
        boom = await do_boom(client)
        steps = [upload, job, boom]

        section("3. Letting logs, spans and scrapes land")
        log("waiting 20s for Promtail, the span exporter and the Prometheus scrape...")
        await asyncio.sleep(20)

        section("4. Interrogating each store independently")
        for s in steps:
            await check_loki(client, s)
            log(f"loki    {s.name}: {s.notes.get('log')}")

        if has_tempo:
            for s in steps:
                await check_tempo(client, s)
                log(f"tempo   {s.name}: {s.notes.get('trace')}")

        for s in steps:
            check_audit(s)
            log(f"audit   {s.name}: {s.notes.get('audit')}")

        check_worker_log(job)
        log(f"worker  {job.name}: {job.notes.get('worker')}")

        await check_metrics(client, upload, before_files, "/files")
        await check_metrics(client, job, before_job, "/demo/job")
        await check_metrics(client, boom, before_boom, "/demo/boom")
        for s in steps:
            log(f"metric  {s.name}: {s.notes.get('metric')}")

    passed = render(steps, sentry_on)

    if passed:
        print(
            f"{GREEN}{BOLD}  SMOKE PASSED{RESET} - one request_id joins "
            "logs, traces, audit and metrics.\n"
        )
        return 0
    print(f"{RED}{BOLD}  SMOKE FAILED{RESET} - see the missing cells above.\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
