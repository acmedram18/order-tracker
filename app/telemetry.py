import os
import time

import structlog
from opentelemetry import context as context_api
from opentelemetry import metrics, trace
from opentelemetry._logs import SeverityNumber, get_logger_provider, set_logger_provider
from opentelemetry._logs import LogRecord
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from prometheus_client import Counter, Gauge
from prometheus_fastapi_instrumentator import Instrumentator


logger = structlog.get_logger("order_tracker")
TRACER_NAME = "order-tracker"
SEVERITIES = {
    "debug": SeverityNumber.DEBUG,
    "info": SeverityNumber.INFO,
    "warning": SeverityNumber.WARN,
    "error": SeverityNumber.ERROR,
    "critical": SeverityNumber.FATAL,
}

orders_created_total = Counter(
    "orders_created_total", "Orders created", ["priority"]
)
order_status_changes_total = Counter(
    "order_status_changes_total", "Order status changes", ["status"]
)
express_orders_overdue = Gauge(
    "express_orders_overdue", "Express orders past their estimated delivery"
)

_tracing_configured = False
_metrics_configured = False
_meter_provider = None
_log_provider = None
_lookup_counter = None
_lookup_duration = None


def _inject_trace_ids(_logger, _method_name, event_dict):
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = format(span_context.trace_id, "032x")
        event_dict["span_id"] = format(span_context.span_id, "016x")
    return event_dict


def _emit_otel_log(_logger, _method_name, event_dict):
    span_context = trace.get_current_span().get_span_context()
    attributes = {}
    for key, value in event_dict.items():
        if key in ("event", "level", "timestamp"):
            continue
        attributes[key] = (
            value if isinstance(value, (str, bool, int, float)) else str(value)
        )
    get_logger_provider().get_logger(TRACER_NAME).emit(
        LogRecord(
            timestamp=time.time_ns(),
            observed_timestamp=time.time_ns(),
            context=context_api.get_current() if span_context.is_valid else None,
            trace_id=span_context.trace_id if span_context.is_valid else 0,
            span_id=span_context.span_id if span_context.is_valid else 0,
            severity_text=event_dict.get("level", "info"),
            severity_number=SEVERITIES.get(
                event_dict.get("level"), SeverityNumber.INFO
            ),
            body=event_dict.get("event", ""),
            attributes=attributes,
        )
    )
    return event_dict


def configure_logging():
    processors = [
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _inject_trace_ids,
    ]
    if os.getenv("OTEL_LOGS_ENABLED") == "true":
        processors.append(_emit_otel_log)
    processors.append(structlog.processors.JSONRenderer())
    structlog.configure(processors=processors)


def configure_otel_logs():
    global _log_provider
    if _log_provider is not None or os.getenv("OTEL_SDK_DISABLED") == "true":
        return
    if os.getenv("OTEL_LOGS_ENABLED") != "true":
        return
    try:
        _log_provider = LoggerProvider(resource=service_resource())
        _log_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(
                    endpoint=os.getenv(
                        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
                    )
                )
            )
        )
        set_logger_provider(_log_provider)
        logger.info("otel_logs_configured")
    except Exception:
        logger.warning("otel_logs_setup_failed", exc_info=True)


def configure_tracing(app):
    global _tracing_configured
    if _tracing_configured:
        return
    _tracing_configured = True
    if os.getenv("OTEL_SDK_DISABLED") == "true":
        logger.info("tracing_disabled")
        return
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    to_console = console_signals_enabled()
    try:
        tracer_provider = TracerProvider(resource=service_resource())
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
        if to_console:
            tracer_provider.add_span_processor(
                SimpleSpanProcessor(ConsoleSpanExporter())
            )
        trace.set_tracer_provider(tracer_provider)
        FastAPIInstrumentor.instrument_app(app)
        logger.info("tracing_configured", endpoint=endpoint, console=to_console)
    except Exception:
        logger.warning("tracing_setup_failed", exc_info=True)


def configure_metrics(app):
    global _metrics_configured
    if _metrics_configured:
        return
    _metrics_configured = True
    try:
        Instrumentator(should_group_status_codes=False).instrument(app).expose(
            app, endpoint="/metrics", should_gzip=True
        )
        logger.info("metrics_configured", endpoint="/metrics")
    except Exception:
        logger.warning("metrics_setup_failed", exc_info=True)


def set_express_orders_overdue(count):
    express_orders_overdue.set(count)


def console_signals_enabled():
    return os.getenv("CONSOLE_SIGNALS_ENABLED") == "true"


def service_resource():
    return Resource(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", "order-tracker"),
            "deployment.environment": os.getenv("DEPLOYMENT_ENVIRONMENT", "local"),
        }
    )


def configure_otel_metrics():
    global _meter_provider, _lookup_counter, _lookup_duration
    if _meter_provider is not None or os.getenv("OTEL_SDK_DISABLED") == "true":
        return
    if not console_signals_enabled():
        return
    try:
        interval = float(os.getenv("CONSOLE_METRICS_INTERVAL_MS", "5000"))
        _meter_provider = MeterProvider(
            resource=service_resource(),
            metric_readers=[
                PeriodicExportingMetricReader(
                    ConsoleMetricExporter(),
                    export_interval_millis=interval,
                )
            ],
        )
        metrics.set_meter_provider(_meter_provider)
        meter = metrics.get_meter(TRACER_NAME)
        _lookup_counter = meter.create_counter(
            "order.lookups", unit="{lookup}", description="Order lookups by route"
        )
        _lookup_duration = meter.create_histogram(
            "order.lookup.duration",
            unit="s",
            description="Time spent looking up an order",
        )
        logger.info("otel_metrics_configured", console_interval_ms=interval)
    except Exception:
        logger.warning("otel_metrics_setup_failed", exc_info=True)


def record_lookup(route, status_code, duration_seconds):
    attributes = {"http.route": route, "http.status_code": status_code}
    if _lookup_counter is not None:
        _lookup_counter.add(1, attributes)
    if _lookup_duration is not None:
        _lookup_duration.record(duration_seconds, attributes)


async def shutdown_telemetry():
    try:
        trace.get_tracer_provider().force_flush()
        trace.get_tracer_provider().shutdown()
    except Exception:
        logger.warning("telemetry_shutdown_failed", exc_info=True)
    if _meter_provider is not None:
        try:
            _meter_provider.force_flush()
            _meter_provider.shutdown()
        except Exception:
            logger.warning("meter_shutdown_failed", exc_info=True)
    if _log_provider is not None:
        try:
            _log_provider.force_flush()
            _log_provider.shutdown()
        except Exception:
            logger.warning("logs_shutdown_failed", exc_info=True)