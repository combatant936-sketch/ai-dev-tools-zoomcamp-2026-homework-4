"""
OpenTelemetry setup.

By default all three signals are exported to an OTLP/gRPC endpoint whose
address is read from the environment variable OTEL_EXPORTER_OTLP_ENDPOINT
(default: http://localhost:4317).

Set OTEL_EXPORTER_OTLP_ENDPOINT=console to fall back to stdout-only
exporters (handy for running the app outside Docker).
"""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

SERVICE_NAME = "order-tracker"

_resource = Resource.create({"service.name": SERVICE_NAME})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _otlp_endpoint() -> str:
    """Return the OTLP gRPC endpoint, or the special sentinel 'console'."""
    return os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")


def _use_console() -> bool:
    return _otlp_endpoint().lower() == "console"


# ---------------------------------------------------------------------------
# Signal setup
# ---------------------------------------------------------------------------

def _setup_traces() -> TracerProvider:
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    provider = TracerProvider(resource=_resource)

    if _use_console():
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        exporter = ConsoleSpanExporter()
    else:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        exporter = OTLPSpanExporter(endpoint=_otlp_endpoint(), insecure=True)

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider


def _setup_metrics() -> "metrics.MeterProvider":
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    if _use_console():
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
        exporter = ConsoleMetricExporter()
    else:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        exporter = OTLPMetricExporter(endpoint=_otlp_endpoint(), insecure=True)

    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=15_000)
    provider = MeterProvider(resource=_resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)
    return provider


def _setup_logs() -> LoggerProvider:
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    provider = LoggerProvider(resource=_resource)

    if _use_console():
        from opentelemetry.sdk._logs.export import ConsoleLogExporter
        exporter = ConsoleLogExporter()
    else:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        exporter = OTLPLogExporter(endpoint=_otlp_endpoint(), insecure=True)

    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(provider)
    return provider


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_tracer_provider: TracerProvider | None = None
_meter_provider = None
_logger_provider: LoggerProvider | None = None


def configure() -> None:
    """Initialise all three OTel signals. Call once at app startup."""
    global _tracer_provider, _meter_provider, _logger_provider
    _tracer_provider = _setup_traces()
    _meter_provider = _setup_metrics()
    _logger_provider = _setup_logs()

    # Bridge stdlib logging → OTel logs pipeline
    from opentelemetry.sdk._logs import LoggingHandler  # noqa: PLC0415
    otel_handler = LoggingHandler(level=logging.NOTSET, logger_provider=_logger_provider)
    logging.getLogger().addHandler(otel_handler)

    mode = "console" if _use_console() else _otlp_endpoint()
    logging.getLogger(__name__).info("OpenTelemetry configured – exporting to %s", mode)


def instrument_app(app) -> None:
    """Attach the FastAPI auto-instrumentor to *app*."""
    FastAPIInstrumentor.instrument_app(app)


def get_tracer(name: str = SERVICE_NAME):
    return trace.get_tracer(name)


def get_meter(name: str = SERVICE_NAME):
    return metrics.get_meter(name)
