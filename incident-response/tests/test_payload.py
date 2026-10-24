import json

import pytest

from incident_response.payload import PayloadError, parse_webhook

from conftest import NOW, grafana_alert, grafana_payload


def parse(payload, now=NOW):
    return parse_webhook(json.dumps(payload).encode(), now, fallback_seconds=900)


def test_parses_grafana_webhook():
    (context,) = parse(grafana_payload())
    assert context.alertname == "5xx responses on order-tracker"
    assert context.severity == "critical"
    assert context.endpoint == "/api/orders/{order_id}"
    assert context.fingerprint == "fp1234567890abcd"
    assert context.status == "firing"
    assert context.resolved is False


def test_window_comes_from_starts_at():
    (context,) = parse(grafana_payload())
    assert context.starts_at.isoformat() == "2026-10-04T03:00:00+00:00"
    assert context.window_seconds == 600


def test_endpoint_falls_back_to_summary_when_no_label():
    payload = grafana_payload(
        grafana_alert(handler=None, summary="5xx responses on /api/orders in order-tracker")
    )
    (context,) = parse(payload)
    assert context.endpoint == "/api/orders"


def test_service_name_is_not_mistaken_for_an_endpoint():
    """The default summary is "5xx responses on order-tracker"; that is not a route."""
    (context,) = parse(grafana_payload(grafana_alert(handler=None)))
    assert context.endpoint is None


def test_endpoint_from_alertname_style_summary_is_ignored():
    (context,) = parse(
        grafana_payload(grafana_alert(handler=None, summary="5xx responses on order-tracker API"))
    )
    assert context.endpoint is None


def test_trailing_punctuation_is_stripped():
    (context,) = parse(
        grafana_payload(grafana_alert(handler=None, summary="5xx responses on /api/orders."))
    )
    assert context.endpoint == "/api/orders"


def test_endpoint_is_none_when_nothing_matches():
    payload = grafana_payload(grafana_alert(handler=None, summary="something odd"))
    (context,) = parse(payload)
    assert context.endpoint is None


def test_fingerprint_generated_from_labels_when_absent():
    entry = grafana_alert(fingerprint="")
    (context,) = parse(grafana_payload(entry))
    assert len(context.fingerprint) == 16
    assert all(char in "0123456789abcdef" for char in context.fingerprint)


def test_fingerprint_is_label_order_independent():
    first = grafana_alert(fingerprint="")
    first["labels"] = {"alertname": "a", "severity": "b", "handler": "/x"}
    second = grafana_alert(fingerprint="")
    second["labels"] = {"handler": "/x", "severity": "b", "alertname": "a"}
    assert parse(grafana_payload(first))[0].fingerprint == parse(grafana_payload(second))[0].fingerprint


def test_resolved_alert_is_flagged():
    payload = grafana_payload(grafana_alert(status="resolved", ends_at="2026-10-04T03:05:00Z"))
    (context,) = parse(payload)
    assert context.resolved is True


def test_zero_timestamp_ends_at_is_treated_as_open():
    (context,) = parse(grafana_payload(grafana_alert(ends_at="0001-01-01T00:00:00Z")))
    assert context.window_seconds == 600


def test_missing_starts_at_uses_fallback_window():
    entry = grafana_alert(starts_at="")
    (context,) = parse(grafana_payload(entry))
    assert context.window_seconds == 900


def test_accepts_alertmanager_shape():
    from conftest import alertmanager_payload

    (context,) = parse(alertmanager_payload())
    assert context.alertname == "5xx responses on order-tracker"


def test_extra_unknown_keys_are_tolerated():
    payload = grafana_payload()
    payload["somethingNew"] = {"nested": True}
    payload["alerts"][0]["futureField"] = 123
    (context,) = parse(payload)
    assert context.endpoint == "/api/orders/{order_id}"


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b'"a string"', b"{}", b'{"alerts": "nope"}', b'{"alerts": [1]}'],
)
def test_invalid_payloads_raise(body):
    with pytest.raises(PayloadError):
        parse_webhook(body, NOW, 900)


def test_values_and_annotations_are_preserved():
    (context,) = parse(grafana_payload())
    assert context.values["A"] == 5.21
    assert context.annotations["description"].startswith("The order-tracker API")
    assert context.generator_url.endswith("/view")


def test_summary_prefers_summary_annotation_over_description():
    (context,) = parse(
        grafana_payload(grafana_alert(summary="short summary", description="long description"))
    )
    assert context.summary == "short summary"
    assert context.description == "long description"
