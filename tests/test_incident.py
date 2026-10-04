import json
import sqlite3
import threading
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient

from app.incident import main, store


FIRING_LABELS = {"alertname": "OrderTrackerDown", "severity": "critical"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "incidents.db")
    monkeypatch.delenv("INCIDENT_WEBHOOK_URL", raising=False)
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def notifications(monkeypatch):
    calls = []

    def record(url, incident):
        calls.append((url, incident))

    monkeypatch.setattr(main, "send_notification", record)
    monkeypatch.setenv("INCIDENT_WEBHOOK_URL", "http://notifier.internal/hook")
    return calls


def alert(status="firing", fingerprint="abc123", labels=None, annotations=None):
    entry = {
        "status": status,
        "labels": dict(FIRING_LABELS if labels is None else labels),
        "annotations": (
            {"description": "orders stopped arriving"}
            if annotations is None
            else annotations
        ),
        "startsAt": "2026-10-03T12:00:00.000Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "http://order-tracker:8000/probe",
    }
    if fingerprint is not None:
        entry["fingerprint"] = fingerprint
    return entry


def webhook(*entries, **extra):
    payload = {
        "version": "4",
        "groupKey": '{}:{alertname="OrderTrackerDown"}',
        "status": "firing" if any(e["status"] == "firing" for e in entries) else "resolved",
        "receiver": "incident-responder",
        "groupLabels": {"alertname": "OrderTrackerDown"},
        "commonLabels": dict(FIRING_LABELS),
        "commonAnnotations": {"summary": "1 alert firing"},
        "externalURL": "http://alertmanager:9093",
        "alerts": list(entries),
    }
    payload.update(extra)
    return payload


def post_alerts(client, payload):
    return client.post("/alerts", json=payload)


def only_incident(client):
    incidents = client.get("/incidents").json()
    assert len(incidents) == 1
    return incidents[0]


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_firing_alert_opens_incident(client):
    response = post_alerts(client, webhook(alert()))
    assert response.status_code == 200
    assert response.json() == {
        "received": 1,
        "opened": 1,
        "updated": 0,
        "resolved": 0,
    }
    incident = only_incident(client)
    assert incident["status"] == "firing"
    assert incident["fingerprint"] == "abc123"
    assert incident["alertname"] == "OrderTrackerDown"
    assert incident["severity"] == "critical"
    assert incident["summary"] == "orders stopped arriving"
    assert incident["notification_count"] == 1
    assert incident["resolved_at"] is None
    assert incident["notified"] == 0
    assert incident["first_seen"] == incident["last_seen"]


def test_repeat_fingerprint_updates_same_incident(client):
    post_alerts(client, webhook(alert()))
    response = post_alerts(client, webhook(alert()))
    assert response.status_code == 200
    assert response.json()["updated"] == 1
    assert response.json()["opened"] == 0
    incident = only_incident(client)
    assert incident["notification_count"] == 2
    assert incident["status"] == "firing"


def test_resolved_alert_closes_incident(client):
    post_alerts(client, webhook(alert()))
    response = post_alerts(client, webhook(alert(status="resolved")))
    assert response.json()["resolved"] == 1
    incident = only_incident(client)
    assert incident["status"] == "resolved"
    assert incident["resolved_at"]


def test_firing_after_resolved_reopens_incident(client):
    post_alerts(client, webhook(alert()))
    post_alerts(client, webhook(alert(status="resolved")))
    response = post_alerts(client, webhook(alert()))
    assert response.json()["updated"] == 1
    incident = only_incident(client)
    assert incident["status"] == "firing"
    assert incident["resolved_at"] is None
    assert incident["notification_count"] == 2


def test_distinct_fingerprints_create_separate_incidents(client):
    response = post_alerts(
        client, webhook(alert(fingerprint="abc123"), alert(fingerprint="def456"))
    )
    assert response.json()["opened"] == 2
    incidents = client.get("/incidents").json()
    assert {incident["fingerprint"] for incident in incidents} == {
        "abc123",
        "def456",
    }


def test_missing_fingerprint_dedups_on_repeat(client):
    first = post_alerts(client, webhook(alert(fingerprint=None)))
    assert first.json()["opened"] == 1
    second = post_alerts(client, webhook(alert(fingerprint=None)))
    assert second.json()["updated"] == 1
    incident = only_incident(client)
    assert incident["notification_count"] == 2
    assert len(incident["fingerprint"]) == main.FINGERPRINT_LENGTH


def test_fallback_fingerprint_ignores_label_order(client):
    labels = {"alertname": "OrderTrackerDown", "severity": "critical", "job": "api"}
    post_alerts(client, webhook(alert(fingerprint=None, labels=labels)))
    reordered = {"job": "api", "severity": "critical", "alertname": "OrderTrackerDown"}
    response = post_alerts(client, webhook(alert(fingerprint=None, labels=reordered)))
    assert response.json()["updated"] == 1
    incident = only_incident(client)
    assert incident["fingerprint"] == main.alert_fingerprint(
        main.Alert(labels=labels)
    )


def test_alertname_and_summary_fall_back(client):
    response = post_alerts(
        client,
        webhook(
            alert(
                fingerprint=None,
                labels={"team": "platform"},
                annotations={"summary": "queue depth high"},
            )
        ),
    )
    assert response.json()["opened"] == 1
    incident = only_incident(client)
    assert incident["alertname"] == "unknown"
    assert incident["severity"] == "none"
    assert incident["summary"] == "queue depth high"


def test_summary_falls_back_to_alertname_without_annotations(client):
    post_alerts(client, webhook(alert(annotations={})))
    assert only_incident(client)["summary"] == "OrderTrackerDown"


def test_malformed_alerts_value_returns_400(client):
    response = post_alerts(client, {"alerts": "not-a-list"})
    assert response.status_code == 400


def test_missing_alerts_key_returns_400(client):
    response = post_alerts(client, {"version": "4", "status": "firing"})
    assert response.status_code == 400


def test_non_object_body_returns_400(client):
    response = client.post(
        "/alerts", content="[1, 2]", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400


def test_invalid_json_returns_400(client):
    response = client.post(
        "/alerts", content="{nope", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400


def test_extra_top_level_keys_are_tolerated(client):
    payload = webhook(
        alert(),
        truncatedAlerts=1,
        someFutureField={"nested": True},
    )
    response = post_alerts(client, payload)
    assert response.status_code == 200
    assert response.json()["opened"] == 1


def test_incidents_list_parses_labels(client):
    post_alerts(
        client,
        webhook(
            alert(fingerprint="abc123"),
            alert(fingerprint="def456", labels={"alertname": "SlowQueries"}),
        ),
    )
    incidents = client.get("/incidents").json()
    assert len(incidents) == 2
    assert all(isinstance(incident["labels"], dict) for incident in incidents)
    assert incidents[0]["labels"]["alertname"] in {
        "OrderTrackerDown",
        "SlowQueries",
    }


def test_get_incident_and_missing_incident(client):
    post_alerts(client, webhook(alert()))
    incident_id = only_incident(client)["id"]
    assert client.get(f"/incidents/{incident_id}").json()["id"] == incident_id
    assert client.get("/incidents/missing").status_code == 404


def test_incidents_listed_newest_first(client):
    post_alerts(client, webhook(alert(fingerprint="abc123")))
    post_alerts(client, webhook(alert(fingerprint="def456")))
    incidents = client.get("/incidents").json()
    assert [incident["fingerprint"] for incident in incidents] == [
        "def456",
        "abc123",
    ]


def test_notification_sent_when_webhook_configured(client, notifications):
    response = post_alerts(client, webhook(alert()))
    assert response.status_code == 200
    assert len(notifications) == 1
    url, incident = notifications[0]
    assert url == "http://notifier.internal/hook"
    assert incident["fingerprint"] == "abc123"
    assert only_incident(client)["notified"] == 1


def test_notification_failure_keeps_response_and_incident(
    client, monkeypatch, notifications
):
    def explode(url, incident):
        raise OSError("connection refused")

    monkeypatch.setattr(main, "send_notification", explode)
    response = post_alerts(client, webhook(alert()))
    assert response.status_code == 200
    assert response.json()["opened"] == 1
    incident = only_incident(client)
    assert incident["notified"] == 0
    assert incident["status"] == "firing"


def test_no_notification_without_webhook_url(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        main, "send_notification", lambda url, incident: calls.append(url)
    )
    response = post_alerts(client, webhook(alert()))
    assert response.json()["opened"] == 1
    assert calls == []
    assert only_incident(client)["notified"] == 0


def test_send_notification_posts_json(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def read(self):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("INCIDENT_WEBHOOK_TIMEOUT", "1.5")
    incident = {
        "id": "incident-1",
        "fingerprint": "abc123",
        "alertname": "OrderTrackerDown",
        "severity": "critical",
        "summary": "orders stopped arriving",
        "status": "firing",
        "notification_count": 1,
        "first_seen": "2026-10-03T12:00:00+00:00",
        "last_seen": "2026-10-03T12:00:00+00:00",
    }
    main.send_notification("http://notifier.internal/hook", incident)
    request = captured["request"]
    assert request.full_url == "http://notifier.internal/hook"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("User-agent") == main.USER_AGENT
    assert captured["timeout"] == 1.5
    assert json.loads(request.data) == {
        "incident_id": "incident-1",
        "fingerprint": "abc123",
        "alertname": "OrderTrackerDown",
        "severity": "critical",
        "summary": "orders stopped arriving",
        "status": "firing",
        "notification_count": 1,
        "first_seen": "2026-10-03T12:00:00+00:00",
        "last_seen": "2026-10-03T12:00:00+00:00",
    }


def test_resolve_unknown_fingerprint_is_counted(client):
    response = post_alerts(client, webhook(alert(status="resolved")))
    assert response.json() == {
        "received": 1,
        "opened": 0,
        "updated": 0,
        "resolved": 1,
    }
    assert client.get("/incidents").json() == []


def test_alert_failure_does_not_break_batch(client, monkeypatch):
    def explode(alert_entry, now):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(main, "handle_alert", explode)
    response = post_alerts(client, webhook(alert(), alert(fingerprint="def456")))
    assert response.status_code == 200
    assert response.json() == {
        "received": 2,
        "opened": 0,
        "updated": 0,
        "resolved": 0,
    }


def test_sql_injection_attempt_is_stored_literally(client):
    hostile = "'; DROP TABLE incidents; --"
    post_alerts(client, webhook(alert(fingerprint=hostile)))
    response = post_alerts(client, webhook(alert(fingerprint=hostile)))
    assert response.json()["updated"] == 1
    assert client.get("/healthz").json() == {"status": "ok"}
    assert only_incident(client)["fingerprint"] == hostile

def test_concurrent_open_incident_creates_single_row(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "incidents.db")
    store.init_db()
    results = []
    barrier = threading.Barrier(8)

    def open_once():
        barrier.wait()
        results.append(
            store.open_incident(
                "race-fp", "RaceAlert", "critical", "race", {"a": "b"}, "now"
            )
        )

    threads = [threading.Thread(target=open_once) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(store.list_incidents()) == 1
    assert sum(1 for r in results if r["created"]) == 1
    assert store.list_incidents()[0]["notification_count"] == 8


def test_webhook_delivery_does_not_block_the_event_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "incidents.db")
    monkeypatch.setenv("INCIDENT_WEBHOOK_URL", "http://notifier.internal/hook")
    release = threading.Event()
    monkeypatch.setattr(
        main, "send_notification", lambda url, incident: release.wait(2.0)
    )
    payload = {"alerts": [alert(fingerprint="block-fp")]}

    with TestClient(main.app) as test_client:
        worker = threading.Thread(
            target=test_client.post, args=("/alerts",), kwargs={"json": payload}
        )
        worker.start()
        time.sleep(0.4)
        started = time.perf_counter()
        test_client.get("/healthz")
        latency = time.perf_counter() - started
        release.set()
        worker.join()

    assert latency < 0.5


def test_fingerprint_index_is_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "incidents.db")
    store.init_db()
    with sqlite3.connect(store.DB_PATH) as db:
        indexes = dict(
            db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
                " AND tbl_name = 'incidents'"
            ).fetchall()
        )
    assert "UNIQUE" in indexes["incidents_fingerprint_idx"].upper()


def test_init_db_upgrades_a_legacy_non_unique_index(tmp_path, monkeypatch):
    db_path = tmp_path / "incidents.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db()
    with sqlite3.connect(db_path) as db:
        db.execute("DROP INDEX incidents_fingerprint_idx")
        db.execute("CREATE INDEX incidents_fingerprint_idx ON incidents (fingerprint)")
        db.execute(
            "INSERT INTO incidents (id, fingerprint, alertname, severity, summary,"
            " status, labels, notification_count, first_seen, last_seen, notified)"
            " VALUES ('dup-a', 'same-fp', 'A', 'warning', 'first', 'resolved', '{}',"
            " 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 0)"
        )
        db.execute(
            "INSERT INTO incidents (id, fingerprint, alertname, severity, summary,"
            " status, labels, notification_count, first_seen, last_seen, notified)"
            " VALUES ('dup-b', 'same-fp', 'A', 'warning', 'second', 'firing', '{}',"
            " 1, '2026-02-01T00:00:00+00:00', '2026-02-01T00:00:00+00:00', 0)"
        )

    store.init_db()

    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT id FROM incidents WHERE fingerprint = 'same-fp'"
        ).fetchall()
        index_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'incidents_fingerprint_idx'"
        ).fetchone()[0]
    assert [row[0] for row in rows] == ["dup-a"]
    assert "UNIQUE" in index_sql.upper()


def test_duplicate_fingerprint_reopens_instead_of_inserting(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "incidents.db")
    store.init_db()
    first = store.open_incident("fp", "A", "warning", "one", {}, "t1")
    store.resolve_incident("fp", "t2")
    second = store.open_incident("fp", "A", "warning", "two", {}, "t3")

    assert first["created"] is True
    assert second["created"] is False
    assert second["incident"]["id"] == first["incident"]["id"]
    assert second["incident"]["status"] == "firing"
    assert second["incident"]["resolved_at"] is None
    assert len(store.list_incidents()) == 1
