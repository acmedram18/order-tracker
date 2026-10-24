import json

from incident_response.store import STATUS_FIRING, STATUS_RESOLVED

from conftest import NOW, grafana_alert, grafana_payload


def context(**kwargs):
    from incident_response.payload import parse_webhook

    return parse_webhook(json.dumps(grafana_payload(grafana_alert(**kwargs))).encode(), NOW, 900)[0]


def test_open_incident_creates_row(store):
    created, incident = store.open_incident(context(), "2026-10-04T03:10:00+00:00")
    assert created is True
    assert incident["status"] == STATUS_FIRING
    assert incident["endpoint"] == "/api/orders/{order_id}"
    assert incident["notification_count"] == 1
    assert incident["first_seen"] == incident["last_seen"]


def test_repeat_alert_updates_and_counts(store):
    store.open_incident(context(), "2026-10-04T03:10:00+00:00")
    created, incident = store.open_incident(context(), "2026-10-04T03:11:00+00:00")
    assert created is False
    assert incident["notification_count"] == 2
    assert incident["last_seen"] == "2026-10-04T03:11:00+00:00"


def test_resolve_then_refire_reopens_same_row(store):
    store.open_incident(context(), "2026-10-04T03:10:00+00:00")
    resolved = store.resolve_incident("fp1234567890abcd", "2026-10-04T03:12:00+00:00")
    assert resolved["status"] == STATUS_RESOLVED
    assert resolved["resolved_at"] == "2026-10-04T03:12:00+00:00"

    created, incident = store.open_incident(context(), "2026-10-04T03:13:00+00:00")
    assert created is False
    assert incident["status"] == STATUS_FIRING
    assert incident["resolved_at"] is None


def test_resolve_unknown_fingerprint_returns_none(store):
    assert store.resolve_incident("nope", "2026-10-04T03:12:00+00:00") is None


def test_distinct_fingerprints_are_separate_incidents(store):
    store.open_incident(context(), "2026-10-04T03:10:00+00:00")
    store.open_incident(context(fingerprint="other123"), "2026-10-04T03:10:00+00:00")
    assert len(store.list_incidents()) == 2


def test_list_is_newest_first(store):
    store.open_incident(context(fingerprint="aaa"), "2026-10-04T03:10:00+00:00")
    store.open_incident(context(fingerprint="bbb"), "2026-10-04T03:20:00+00:00")
    assert [row["fingerprint"] for row in store.list_incidents()] == ["bbb", "aaa"]


def test_agent_result_is_recorded(store):
    _, incident = store.open_incident(context(), "2026-10-04T03:10:00+00:00")
    store.set_agent_result(incident["id"], "finished", "ses_abc", 0)
    updated = store.get_incident(incident["id"])
    assert updated["agent_status"] == "finished"
    assert updated["agent_session_id"] == "ses_abc"
    assert updated["agent_exit_code"] == 0


def test_running_agents_are_reconciled_on_restart(store):
    _, incident = store.open_incident(context(), "2026-10-04T03:10:00+00:00")
    store.set_agent_result(incident["id"], "running")
    assert store.mark_running_agents_interrupted() == 1
    assert store.get_incident(incident["id"])["agent_status"] == "interrupted"
    # idempotent: a second startup has nothing left to fix
    assert store.mark_running_agents_interrupted() == 0


def test_reconciliation_leaves_finished_runs_alone(store):
    _, incident = store.open_incident(context(), "2026-10-04T03:10:00+00:00")
    store.set_agent_result(incident["id"], "finished")
    assert store.mark_running_agents_interrupted() == 0
    assert store.get_incident(incident["id"])["agent_status"] == "finished"


def test_write_bundle_creates_all_files(store):
    from incident_response.evidence import Evidence
    from incident_response.bundle import render
    from incident_response.config import Settings

    settings = Settings()
    alert = context()
    _, incident = store.open_incident(alert, "2026-10-04T03:10:00+00:00")
    found = Evidence(endpoint=alert.endpoint, logs=[], traces=[])
    directory = store.bundle_dir(incident["id"], alert.fingerprint, alert.starts_at.isoformat())
    markdown = render(settings, alert, found, incident["id"])
    files = store.write_bundle(directory, alert, found, incident["id"], markdown)

    assert set(files) == {"alert", "logs", "traces", "metrics", "bundle"}
    payload = json.loads(files["alert"].read_text())
    assert payload["fingerprint"] == alert.fingerprint
    assert payload["endpoint"] == "/api/orders/{order_id}"
    assert "Incident" in files["bundle"].read_text()
