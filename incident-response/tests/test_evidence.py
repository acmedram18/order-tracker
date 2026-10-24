import json
from datetime import datetime, timezone

import pytest

from incident_response import evidence
from incident_response.evidence import collect_logs, fetch_trace, parse_trace

START = datetime(2026, 10, 4, 3, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 4, 3, 5, 0, tzinfo=timezone.utc)

LOKI_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "streams",
        "result": [
            {
                "stream": {
                    "service_name": "order-tracker",
                    "route": "/api/orders/{order_id}",
                    "severity_text": "error",
                    "http_status_code": "500",
                    "trace_id": "aaaa1111",
                    "span_id": "bbbb2222",
                },
                "values": [["1791083423127927975", "order_failed"]],
            },
            {
                "stream": {
                    "service_name": "order-tracker",
                    "route": "/api/orders",
                    "severity_text": "info",
                    "http_status_code": "201",
                    "trace_id": "cccc3333",
                },
                "values": [["1791083423127927900", "order_created"]],
            },
        ],
    },
}

TEMPO_TRACE = {
    "batches": [
        {
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "order-tracker"}}]},
            "scopeSpans": [
                {
                    "spans": [
                        {
                            "traceId": "aaaa1111",
                            "spanId": "bbbb2222",
                            "name": "GET /api/orders/{order_id}",
                            "startTimeUnixNano": "1000000000",
                            "endTimeUnixNano": "1500000000",
                            "attributes": [{"key": "http.route", "value": {"stringValue": "/api/orders/{order_id}"}}],
                            "status": {"code": "STATUS_CODE_ERROR"},
                        },
                        {
                            "traceId": "aaaa1111",
                            "spanId": "dddd4444",
                            "name": "order.lookup",
                            "startTimeUnixNano": "1100000000",
                            "endTimeUnixNano": "1400000000",
                            "attributes": [{"key": "http.status_code", "value": {"intValue": "500"}}],
                        },
                    ]
                }
            ],
        }
    ]
}


def stub_json(monkeypatch, handler):
    calls = []

    def fake(url, params, timeout):
        calls.append((url, params))
        return handler(url, params)

    monkeypatch.setattr(evidence, "get_json", fake)
    return calls


def test_logs_are_filtered_by_endpoint_in_python(monkeypatch):
    """`route` is structured metadata, so the filter cannot live in LogQL."""
    calls = stub_json(monkeypatch, lambda url, params: LOKI_RESPONSE)
    entries = collect_logs("http://loki:3100", "order-tracker", START, END, "/api/orders/{order_id}", 50, 5)

    assert [entry.line for entry in entries] == ["order_failed"]
    assert entries[0].trace_id == "aaaa1111"
    assert entries[0].status == "500"
    assert entries[0].level == "error"
    # the query itself must only use indexed labels
    url, params = calls[0]
    assert params["query"] == '{service_name="order-tracker"}'
    assert "route" not in params["query"]


def test_logs_without_endpoint_filter_keep_everything(monkeypatch):
    stub_json(monkeypatch, lambda url, params: LOKI_RESPONSE)
    entries = collect_logs("http://loki:3100", "order-tracker", START, END, None, 50, 5)
    assert {entry.line for entry in entries} == {"order_failed", "order_created"}


def test_unattributed_logs_are_dropped_when_endpoint_has_its_own(monkeypatch):
    """`/api/orders/{order_id}` logs carry `route`; the `/api/orders` noise must not leak in."""
    stub_json(monkeypatch, lambda url, params: LOKI_RESPONSE)
    entries = collect_logs(
        "http://loki:3100", "order-tracker", START, END, "/api/orders/{order_id}", 50, 5
    )
    assert all(entry.route == "/api/orders/{order_id}" for entry in entries)


def test_unattributed_logs_are_used_when_endpoint_emits_none(monkeypatch):
    """Live check: `/api/orders` logs carry no `route`, so a strict filter returns nothing."""
    payload = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service_name": "order-tracker", "severity_text": "debug"},
                    "values": [["1791083423127927975", "orders_listed"]],
                }
            ],
        },
    }
    stub_json(monkeypatch, lambda url, params: payload)
    entries = collect_logs("http://loki:3100", "order-tracker", START, END, "/api/orders", 50, 5)
    assert [entry.line for entry in entries] == ["orders_listed"]
    assert entries[0].route is None


def test_logs_are_newest_first_and_limited(monkeypatch):
    stub_json(monkeypatch, lambda url, params: LOKI_RESPONSE)
    entries = collect_logs("http://loki:3100", "order-tracker", START, END, None, 1, 5)
    assert len(entries) == 1


def test_timestamp_is_converted_to_iso(monkeypatch):
    stub_json(monkeypatch, lambda url, params: LOKI_RESPONSE)
    entry = collect_logs("http://loki:3100", "order-tracker", START, END, "/api/orders/{order_id}", 5, 5)[0]
    assert entry.timestamp.startswith("2026-")


def test_parse_trace_sorts_spans_by_duration():
    trace = parse_trace("aaaa1111", TEMPO_TRACE, span_limit=10)
    assert trace.service == "order-tracker"
    assert trace.root == "GET /api/orders/{order_id}"
    assert [span.name for span in trace.spans] == ["GET /api/orders/{order_id}", "order.lookup"]
    assert trace.spans[0].duration_ms == 500.0
    assert trace.spans[0].attributes["http.route"] == "/api/orders/{order_id}"
    assert trace.spans[1].attributes["http.status_code"] == "500"


def test_parse_trace_respects_span_limit():
    trace = parse_trace("aaaa1111", TEMPO_TRACE, span_limit=1)
    assert len(trace.spans) == 1


def test_fetch_trace_marks_missing_trace(monkeypatch):
    import urllib.error

    def raise_404(url, params, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(evidence, "get_json", raise_404)
    trace = fetch_trace("http://tempo:3200", "missing", 10, 5)
    assert trace.error == "not found in Tempo"


def test_collect_survives_backend_failure(monkeypatch):
    def boom(url, params, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(evidence, "get_json", boom)
    from incident_response.config import Settings
    from incident_response.payload import build_context

    settings = Settings()
    context = build_context(
        {"labels": {"alertname": "x", "handler": "/api/orders"}, "status": "firing"}, START
    )
    found = evidence.collect(settings, context)
    assert found.logs == []
    assert found.traces == []
    assert any("loki:" in problem for problem in found.errors)


def test_collect_follows_logs_to_traces(monkeypatch):
    def handler(url, params):
        if "/loki/" in url:
            return LOKI_RESPONSE
        if "/api/traces/" in url:
            return TEMPO_TRACE
        if "/api/search" in url:
            return {"traces": []}
        if "/api/v1/query" in url:
            return {"status": "success", "data": {"resultType": "vector", "result": []}}
        raise AssertionError(url)

    monkeypatch.setattr(evidence, "get_json", lambda url, params, timeout: handler(url, params))
    from incident_response.config import Settings
    from incident_response.payload import build_context

    context = build_context(
        {
            "labels": {"alertname": "x", "handler": "/api/orders/{order_id}"},
            "status": "firing",
            "startsAt": "2026-10-04T03:00:00Z",
        },
        END,
    )
    found = evidence.collect(Settings(), context)
    assert [entry.trace_id for entry in found.logs] == ["aaaa1111"]
    assert [trace.trace_id for trace in found.traces] == ["aaaa1111"]
    assert found.errors == []
