"""OpenTelemetry and Prometheus wiring.

Kept in one module so `main.py` reads as a list of concerns rather than a pile
of vendor setup. Everything here degrades to a no-op when disabled, so the same
code runs in tests and in production.
"""

from __future__ import annotations

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

from app import __version__
from app.config import Settings
from app.logging import get_logger

log = get_logger(__name__)

_tracing_configured = False

#: The in-progress gauge is namespaced by hand: unlike the other metrics, the
#: instrumentator does NOT apply `metric_namespace` to it.
INPROGRESS_METRIC = "app_http_requests_inprogress"

#: Prometheus collectors are process-global, so instrumentation runs once.
_metrics_instrumented = False


def configure_tracing(config: Settings) -> None:
    """Install a global TracerProvider.

    A provider is installed even with no exporter configured: spans are then
    generated and dropped, which still gives every request a real `trace_id`
    for log correlation. Set ``OTEL_EXPORTER_OTLP_ENDPOINT`` to also ship them.
    """
    global _tracing_configured
    if _tracing_configured or not config.otel_enabled:
        return

    resource = Resource.create(
        {
            SERVICE_NAME: config.otel_service_name,
            SERVICE_VERSION: __version__,
            "deployment.environment": config.environment,
        }
    )
    provider = TracerProvider(resource=resource)

    if config.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(
            endpoint=f"{config.otel_exporter_otlp_endpoint.rstrip('/')}/v1/traces"
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        log.info("otel.exporter.configured", endpoint=config.otel_exporter_otlp_endpoint)
    else:
        # No exporter: spans still exist (so trace_id is real) but go nowhere.
        provider.add_span_processor(SimpleSpanProcessor(_NullSpanExporter()))
        log.info("otel.exporter.none", detail="spans generated for correlation only")

    trace.set_tracer_provider(provider)
    _tracing_configured = True


class _NullSpanExporter:
    """Drops spans. Keeps the provider valid without shipping anything."""

    def export(self, spans: object) -> int:
        from opentelemetry.sdk.trace.export import SpanExportResult

        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def instrument_app(app: FastAPI, config: Settings) -> None:
    """Auto-instrument FastAPI, SQLAlchemy and Redis.

    Instrumentation failures are logged, never raised -- observability must not
    be able to stop the service from starting.
    """
    if not config.otel_enabled:
        return

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls="health/live,health/ready,metrics",
        )
    except Exception as exc:
        log.warning("otel.instrument.fastapi.failed", error=str(exc))

    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        RedisInstrumentor().instrument()
    except Exception as exc:
        log.warning("otel.instrument.redis.failed", error=str(exc))


def instrument_sqlalchemy(engine: object) -> None:
    """Instrument the async engine. Called once the engine exists."""
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=getattr(engine, "sync_engine", engine))
    except Exception as exc:
        log.warning("otel.instrument.sqlalchemy.failed", error=str(exc))


def configure_metrics(app: FastAPI, config: Settings) -> None:
    """Expose Prometheus metrics at ``/metrics``.

    Labels are route/method/status only. Correlation ids are deliberately NOT
    labels -- they are unbounded and would blow up cardinality. Ids live in
    logs and traces; metrics stay aggregate.

    Instrumentation happens **once per process**. Prometheus collectors are
    global, so instrumenting a second application -- which every test suite
    does when it calls ``create_app()`` again -- would raise
    ``DuplicateTimeseries`` on the first request. Later applications still get
    a working ``/metrics`` endpoint; they simply reuse the collectors already
    registered rather than creating a second set.
    """
    if not config.metrics_enabled:
        return

    global _metrics_instrumented

    from prometheus_fastapi_instrumentator import Instrumentator

    instrumentator = Instrumentator(
        should_group_status_codes=False,
        should_ignore_untemplated=True,
        should_instrument_requests_inprogress=True,
        # The instrumentator does not apply `metric_namespace` to this gauge,
        # so it is namespaced by hand to match the other metrics (and the
        # Grafana dashboard, which queries `app_http_requests_inprogress`).
        inprogress_name=INPROGRESS_METRIC,
        inprogress_labels=True,
        excluded_handlers=["/metrics", "/health/live", "/health/ready"],
    )

    if not _metrics_instrumented:
        instrumentator.instrument(
            app,
            # Namespace only. The library's metric names already start with
            # `http_`, so a subsystem would yield `app_http_http_requests_total`.
            metric_namespace="app",
            metric_subsystem="",
        )
        _metrics_instrumented = True
    else:
        log.debug("metrics.instrument.skipped", reason="already instrumented in this process")

    instrumentator.expose(app, endpoint="/metrics", include_in_schema=True, tags=["observability"])
