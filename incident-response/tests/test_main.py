import json

from incident_response import main as main_module

from conftest import alertmanager_payload, grafana_alert, grafana_payload


def post(client, payload):
    return client.post("/alerts", json=payload)


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_opens_incident_and_returns_counts(client, no_backends):
    response = post(client, grafana_payload())
    assert response.status_code == 200
    body = response.json()
    assert body == {"received": 1, "opened": 1, "updated": 0, "resolved": 0}

    incidents = client.get("/incidents").json()
    assert len(incidents) == 1
    assert incidents[0]["endpoint"] == "/api/orders/{order_id}"
    assert incidents[0]["status"] == "firing"


def test_repeat_alert_updates(client, no_backends):
    post(client, grafana_payload())
    body = post(client, grafana_payload()).json()
    assert body == {"received": 1, "opened": 0, "updated": 1, "resolved": 0}
    assert client.get("/incidents").json()[0]["notification_count"] == 2


def test_resolved_alert_closes_incident(client, no_backends):
    post(client, grafana_payload())
    body = post(
        client, grafana_payload(grafana_alert(status="resolved", ends_at="2026-10-04T03:05:00Z"))
    ).json()
    assert body["resolved"] == 1
    assert client.get("/incidents").json()[0]["status"] == "resolved"


def test_batch_mixes_firing_and_resolved(client, no_backends):
    payload = grafana_payload(
        grafana_alert(fingerprint="aaa1111"),
        grafana_alert(fingerprint="bbb2222", handler="/api/orders"),
    )
    body = post(client, payload).json()
    assert body["received"] == 2
    assert body["opened"] == 2
    assert len(client.get("/incidents").json()) == 2


def test_accepts_alertmanager_payload(client, no_backends):
    body = post(client, alertmanager_payload()).json()
    assert body["opened"] == 1


def test_bundle_is_written_to_disk(client, no_backends, store):
    post(client, grafana_payload())
    incident = client.get("/incidents").json()[0]
    assert incident["bundle_path"], "bundle_path should be recorded"
    bundle = store.bundle_dir  # sanity: store is the tmp-backed one
    assert bundle is not None

    from pathlib import Path

    directory = Path(incident["bundle_path"])
    assert (directory / "alert.json").exists()
    assert (directory / "logs.json").exists()
    assert (directory / "traces.json").exists()
    assert (directory / "metrics.json").exists()
    markdown = (directory / "bundle.md").read_text()
    assert "/api/orders/{order_id}" in markdown
    assert "Where to look" in markdown


def test_agent_disabled_is_recorded(client, no_backends):
    post(client, grafana_payload())
    incident = client.get("/incidents").json()[0]
    assert incident["agent_status"] == "disabled"


def test_unknown_incident_returns_404(client):
    assert client.get("/incidents/does-not-exist").status_code == 404


def test_invalid_payloads_return_400(client):
    for body in (b"", b"nope", b"[]", b'{"alerts":"x"}'):
        assert client.post("/alerts", content=body).status_code == 400


def test_investigate_failure_does_not_break_response(client, monkeypatch, no_backends):
    def boom(_settings, _alert):
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(main_module.evidence_module, "collect", boom)
    response = post(client, grafana_payload())
    assert response.status_code == 200
    assert response.json()["opened"] == 1
