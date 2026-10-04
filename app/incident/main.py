import hashlib
import json
import os
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.incident import store
from app.telemetry import configure_logging, logger


DEFAULT_ALERTNAME = "unknown"
DEFAULT_SEVERITY = "none"
FINGERPRINT_LENGTH = 16
MAX_TEXT_LENGTH = 500
SUMMARY_KEYS = ("description", "summary")
STATUS_FIRING = "firing"
STATUS_RESOLVED = "resolved"
USER_AGENT = "order-tracker-incident-responder/1.0"
WEBHOOK_TIMEOUT = 5.0


class Alert(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str = STATUS_FIRING
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str | None = None
    endsAt: str | None = None
    fingerprint: str = ""


class AlertmanagerWebhook(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str = "4"
    groupKey: str = ""
    status: str = STATUS_FIRING
    receiver: str = ""
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str = ""
    alerts: list[Alert]


def webhook_url():
    return os.getenv("INCIDENT_WEBHOOK_URL", "")


def webhook_timeout():
    return float(os.getenv("INCIDENT_WEBHOOK_TIMEOUT", str(WEBHOOK_TIMEOUT)))


def clip(text):
    return text if len(text) <= MAX_TEXT_LENGTH else text[:MAX_TEXT_LENGTH]


def alert_summary(alert, alertname):
    for key in SUMMARY_KEYS:
        value = alert.annotations.get(key)
        if value:
            return clip(value)
    return alertname


def alert_fingerprint(alert):
    if alert.fingerprint:
        return alert.fingerprint
    canonical = json.dumps(alert.labels, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def send_notification(url, incident):
    payload = json.dumps(
        {
            "incident_id": incident["id"],
            "fingerprint": incident["fingerprint"],
            "alertname": incident["alertname"],
            "severity": incident["severity"],
            "summary": incident["summary"],
            "status": incident["status"],
            "notification_count": incident["notification_count"],
            "first_seen": incident["first_seen"],
            "last_seen": incident["last_seen"],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=webhook_timeout()) as response:
        response.read()


def notify_incident(incident):
    url = webhook_url()
    if not url:
        return
    try:
        send_notification(url, incident)
    except Exception:
        logger.warning(
            "incident_notification_failed",
            incident_id=incident["id"],
            fingerprint=incident["fingerprint"],
            exc_info=True,
        )
        return
    store.mark_notified(incident["id"])


def incident_view(incident):
    row = dict(incident)
    row["labels"] = json.loads(row["labels"])
    return row


def empty_counts(received=0):
    return {"received": received, "opened": 0, "updated": 0, "resolved": 0}


def parse_payload(body):
    try:
        payload = json.loads(body) if body else None
    except ValueError as exc:
        logger.warning("alert_payload_malformed", error=clip(str(exc)))
        raise HTTPException(400, "Invalid Alertmanager payload") from None
    if not isinstance(payload, dict):
        logger.warning(
            "alert_payload_not_object", payload_type=type(payload).__name__
        )
        raise HTTPException(400, "Invalid Alertmanager payload")
    return payload


def parse_webhook(body):
    try:
        return AlertmanagerWebhook.model_validate(parse_payload(body))
    except ValueError as exc:
        logger.warning("alert_payload_invalid", error=clip(str(exc)))
        raise HTTPException(400, "Invalid Alertmanager payload") from None


def handle_alert(alert, now):
    alertname = alert.labels.get("alertname") or DEFAULT_ALERTNAME
    severity = alert.labels.get("severity") or DEFAULT_SEVERITY
    summary = alert_summary(alert, alertname)
    fingerprint = alert_fingerprint(alert)
    logger.info(
        "alert_received",
        alertname=alertname,
        severity=severity,
        status=alert.status,
        fingerprint=fingerprint,
    )
    if alert.status == STATUS_RESOLVED:
        resolved = store.resolve_incident(fingerprint, now)
        if resolved is None:
            logger.warning("incident_resolve_ignored", fingerprint=fingerprint)
            return "resolved"
        logger.info(
            "incident_resolved", incident_id=resolved["id"], fingerprint=fingerprint
        )
        return "resolved"
    result = store.open_incident(
        fingerprint, alertname, severity, summary, alert.labels, now
    )
    incident = result["incident"]
    notify_incident(incident)
    if result["created"]:
        logger.info(
            "incident_opened",
            incident_id=incident["id"],
            fingerprint=fingerprint,
            notification_count=incident["notification_count"],
        )
        return "opened"
    logger.info(
        "incident_updated",
        incident_id=incident["id"],
        fingerprint=fingerprint,
        notification_count=incident["notification_count"],
    )
    return "updated"


def handle_alerts(alerts):
    counts = empty_counts(len(alerts))
    now = datetime.now(timezone.utc).isoformat()
    for alert in alerts:
        try:
            counts[handle_alert(alert, now)] += 1
        except Exception:
            logger.warning("alert_processing_failed", exc_info=True)
    return counts


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    logger.info("incident_responder_started", db_path=str(store.DB_PATH))
    yield


app = FastAPI(title="Incident Responder", lifespan=lifespan)

configure_logging()


@app.post("/alerts", status_code=200)
async def receive_alerts(request: Request):
    try:
        webhook = parse_webhook(await request.body())
        return await run_in_threadpool(handle_alerts, webhook.alerts)
    except HTTPException:
        raise
    except Exception:
        logger.warning("alert_delivery_failed", exc_info=True)
        return empty_counts()


@app.get("/incidents")
def list_incidents():
    incidents = store.list_incidents()
    logger.debug("incidents_listed", count=len(incidents))
    return [incident_view(incident) for incident in incidents]


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str):
    incident = store.get_incident(incident_id)
    if incident is None:
        logger.warning("incident_not_found", incident_id=incident_id, method="get")
        raise HTTPException(404, "Incident not found")
    return incident_view(incident)


@app.get("/healthz")
def health():
    with store.connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}