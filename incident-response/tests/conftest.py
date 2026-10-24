import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from incident_response import main as main_module  # noqa: E402
from incident_response.config import Settings  # noqa: E402
from incident_response.store import Store  # noqa: E402


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "false")
    configured = Settings(
        data_dir=tmp_path / "incidents",
        index_db=tmp_path / "incidents.db",
        agent_dir=tmp_path,
    )
    return configured


@pytest.fixture
def store(settings):
    return Store(settings.index_db, settings.bundle_root())


@pytest.fixture
def client(settings, store, monkeypatch):
    from incident_response.agent import AgentRunner

    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "store", store)
    # Replace the runner too: the module-level one is built from the real
    # environment and would spawn live opencode sessions during tests.
    monkeypatch.setattr(main_module, "runner", AgentRunner(settings))
    from fastapi.testclient import TestClient

    with TestClient(main_module.app) as test_client:
        yield test_client


@pytest.fixture
def no_backends(monkeypatch):
    """Replace backend collection so tests never touch Loki/Tempo/Prometheus."""
    from incident_response import evidence as evidence_module
    from incident_response.evidence import Evidence

    def fake_collect(_settings, alert):
        return Evidence(
            endpoint=alert.endpoint,
            window_start=alert.starts_at.isoformat(),
            window_end=alert.ends_at.isoformat() if alert.ends_at else "",
        )

    monkeypatch.setattr(evidence_module, "collect", fake_collect)


def grafana_alert(
    alertname="5xx responses on order-tracker",
    status="firing",
    handler="/api/orders/{order_id}",
    starts_at="2026-10-04T03:00:00Z",
    ends_at="0001-01-01T00:00:00Z",
    fingerprint="fp1234567890abcd",
    severity="critical",
    summary=None,
    description="The order-tracker API returned one or more 5xx responses in the last 5m window.",
):
    labels = {"alertname": alertname, "severity": severity, "service": "order-tracker"}
    if handler:
        labels["handler"] = handler
    annotations = {"description": description}
    if summary is not None:
        annotations["summary"] = summary
    return {
        "status": status,
        "labels": labels,
        "annotations": annotations,
        "startsAt": starts_at,
        "endsAt": ends_at,
        "fingerprint": fingerprint,
        "generatorURL": "http://grafana:3000/alerting/grafana/order-tracker-5xx/view",
        "values": {"A": 5.21, "C": 5.21},
        "valueString": "[ var='A' labels={} type='query' value=5.21 ]",
    }


def grafana_payload(*alerts, status="firing"):
    entries = list(alerts) or [grafana_alert()]
    return {
        "receiver": "incident-response",
        "status": status,
        "alerts": entries,
        "groupLabels": {},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://grafana:3000/",
        "version": "1",
        "groupKey": '{}:{alertname="5xx responses on order-tracker"}',
        "truncatedAlerts": 0,
        "orgId": 1,
        "title": "[FIRING:1] 5xx responses on order-tracker",
        "state": "alerting",
    }


def alertmanager_payload(*alerts):
    return {
        "version": "4",
        "groupKey": '{}:{alertname="test"}',
        "status": "firing" if any(a.get("status") != "resolved" for a in alerts) else "resolved",
        "receiver": "incident-responder",
        "groupLabels": {"alertname": "test"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
        "alerts": list(alerts) or [grafana_alert()],
    }


NOW = datetime(2026, 10, 4, 3, 10, 0, tzinfo=timezone.utc)
