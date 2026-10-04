import os

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Gauge
from prometheus_fastapi_instrumentator import Instrumentator


logger = structlog.get_logger("order_tracker")

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


def _inject_trace_ids(_logger, _method_name, event_dict):
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = format(span_context.trace_id, "032x")
        event_dict["span_id"] = format(span_context.span_id, "016x")
    return event_dict


def configure_logging():
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _inject_trace_ids,
            structlog.processors.JSONRenderer(),
        ],
    )


def configure_tracing(app):
    global _tracing_configured
    if _tracing_configured:
        return
    _tracing_configured = True
    if os.getenv("OTEL_SDK_DISABLED") == "true":
        logger.info("tracing_disabled")
        return
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    try:
        tracer_provider = TracerProvider(
            resource=Resource(
                {
                    "service.name": os.getenv("OTEL_SERVICE_NAME", "order-tracker"),
                    "deployment.environment": os.getenv(
                        "DEPLOYMENT_ENVIRONMENT", "local"
                    ),
                }
            )
        )
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
        trace.set_tracer_provider(tracer_provider)
        FastAPIInstrumentor.instrument_app(app)
        logger.info("tracing_configured", endpoint=endpoint)
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


async def shutdown_telemetry():
    try:
        trace.get_tracer_provider().force_flush()
        trace.get_tracer_provider().shutdown()
    except Exception:
        logger.warning("telemetry_shutdown_failed", exc_info=True)