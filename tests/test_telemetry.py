from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app) as test_client:
        yield test_client


def read_metrics(test_client):
    response = test_client.get("/metrics")
    assert response.status_code == 200
    return response.text


def metric_value(body, name, labels=""):
    for line in body.splitlines():
        if line.startswith(name) and labels in line and not line.startswith("#"):
            return float(line.rsplit(" ", 1)[1])
    return None


def test_metrics_endpoint_serves_prometheus_text(client):
    body = read_metrics(client)
    assert "http_request_duration" in body
    assert "orders_created_total" in body
    assert "express_orders_overdue" in body


def test_create_order_increments_counter(client):
    before = metric_value(
        read_metrics(client), "orders_created_total", 'priority="express"'
    ) or 0.0
    client.post(
        "/api/orders",
        json={"customer": "Jo", "item": "Kettle", "priority": "express"},
    )
    after = metric_value(
        read_metrics(client), "orders_created_total", 'priority="express"'
    ) or 0.0
    assert after - before == 1.0


def test_status_change_increments_counter(client):
    before = metric_value(
        read_metrics(client), "order_status_changes_total", 'status="shipped"'
    ) or 0.0
    order_id = client.post(
        "/api/orders", json={"customer": "Kim", "item": "Lamp"}
    ).json()["id"]
    client.patch(f"/api/orders/{order_id}", json={"status": "shipped"})
    after = metric_value(
        read_metrics(client), "order_status_changes_total", 'status="shipped"'
    ) or 0.0
    assert after - before == 1.0


def test_overdue_gauge_tracks_seeded_month_end_order(client):
    assert metric_value(read_metrics(client), "express_orders_overdue") == 1.0


def test_overdue_gauge_drops_when_order_is_delivered(client):
    client.patch("/api/orders/express-1002", json={"status": "delivered"})
    assert metric_value(read_metrics(client), "express_orders_overdue") == 0.0


def test_seeded_express_order_on_month_end_does_not_error(client):
    response = client.get("/api/orders/express-1002")
    assert response.status_code == 200
    assert response.json()["estimated_delivery"]


def test_estimated_delivery_crosses_month_boundary():
    order = {"created_at": "2026-09-30T12:00:00+00:00"}
    assert main.estimated_delivery(order) == datetime(
        2026, 10, 2, 12, 0, tzinfo=timezone.utc
    )


def test_estimated_delivery_crosses_year_boundary():
    order = {"created_at": "2026-12-31T23:00:00+00:00"}
    assert main.estimated_delivery(order) == datetime(
        2027, 1, 2, 23, 0, tzinfo=timezone.utc
    )


def test_estimated_delivery_treats_naive_timestamps_as_utc():
    order = {"created_at": "2026-10-01T00:00:00"}
    assert main.estimated_delivery(order) == datetime(
        2026, 10, 3, tzinfo=timezone.utc
    )


def test_count_overdue_express_ignores_delivered_orders(client):
    assert main.count_overdue_express() == 1
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    with main.connect() as db:
        db.execute(
            "INSERT INTO orders VALUES ('fresh-express', 'New', 'Box', 'express',"
            " 'received', ?)",
            (recent.isoformat(),),
        )
    assert main.count_overdue_express() == 1

def test_emit_otel_log_uses_context_not_span_context(monkeypatch):
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        InMemoryLogExporter,
        SimpleLogRecordProcessor,
    )
    from opentelemetry.sdk.trace import TracerProvider

    from app import telemetry

    # conftest disables the SDK globally; this test exercises the real log path.
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)

    exporter = InMemoryLogExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    monkeypatch.setattr(telemetry, "get_logger_provider", lambda: provider)

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("order.lookup") as span:
        span_context = span.get_span_context()
        telemetry._emit_otel_log(
            None,
            None,
            {
                "event": "order_lookup",
                "level": "info",
                "order_id": "standard-1001",
                "http_status_code": 200,
            },
        )

    records = exporter.get_finished_logs()
    assert len(records) == 1
    record = records[0].log_record
    assert record.body == "order_lookup"
    assert record.trace_id == span_context.trace_id
    assert record.span_id == span_context.span_id
    assert record.attributes["order_id"] == "standard-1001"
    # LogRecord.context must be an OTel Context (has .get); a SpanContext here
    # raises AttributeError inside the SDK and turns every log into a 500.
    assert hasattr(record.context, "get")


def test_console_metrics_are_off_unless_enabled(monkeypatch):
    from app import telemetry

    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("CONSOLE_SIGNALS_ENABLED", raising=False)
    monkeypatch.setattr(telemetry, "_meter_provider", None)
    telemetry.configure_otel_metrics()
    assert telemetry._meter_provider is None

    monkeypatch.setenv("CONSOLE_SIGNALS_ENABLED", "true")
    monkeypatch.setattr(telemetry, "_lookup_counter", None)
    telemetry.configure_otel_metrics()
    assert telemetry._meter_provider is not None
