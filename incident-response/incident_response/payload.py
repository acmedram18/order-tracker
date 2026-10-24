"""Parsing of Grafana unified alerting webhooks (and Alertmanager v4 payloads).

Grafana and Alertmanager both POST a top-level object with an ``alerts`` array,
so one parser covers both. Unknown keys are ignored on purpose: both senders add
fields between versions and a monitoring endpoint should not 400 on them.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

FINGERPRINT_LENGTH = 16
MAX_TEXT_LENGTH = 500
SUMMARY_KEYS = ("summary",)
DESCRIPTION_KEYS = ("description",)
ENDPOINT_LABEL_KEYS = ("handler", "route", "endpoint", "path")
ENDPOINT_PATTERNS = (
    re.compile(r"5xx responses on (?P<endpoint>\S+?)(?:\s+in\b|$)"),
    re.compile(r"\bon (?P<endpoint>/\S+?)(?:\s+in\b|\s*$|\s*\()"),
)
MIN_TIMESTAMP = datetime(1, 1, 1, tzinfo=timezone.utc)
RESOLVED = "resolved"
FIRING = "firing"


class PayloadError(ValueError):
    """Raised when the request body is not a usable alert webhook payload."""


@dataclass
class AlertContext:
    fingerprint: str
    alertname: str
    severity: str
    status: str
    endpoint: str | None
    summary: str
    description: str
    starts_at: datetime
    ends_at: datetime | None
    window_seconds: int
    labels: dict = field(default_factory=dict)
    annotations: dict = field(default_factory=dict)
    values: dict = field(default_factory=dict)
    generator_url: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == RESOLVED


def _clip(value: str, limit: int = MAX_TEXT_LENGTH) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit]


def _parse_timestamp(raw) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith(("z", "Z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _text_field(annotations: dict, keys=SUMMARY_KEYS, default: str = "") -> str:
    for key in keys:
        if key in annotations:
            return _clip(annotations[key])
    return default


def alert_fingerprint(alert: dict, labels: dict) -> str:
    provided = alert.get("fingerprint")
    if provided:
        return str(provided)
    canonical = json.dumps(labels, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:FINGERPRINT_LENGTH]


def alert_endpoint(labels: dict, summary: str, description: str) -> str | None:
    for key in ENDPOINT_LABEL_KEYS:
        value = labels.get(key)
        if value:
            return str(value)
    for text in (summary, description):
        if not text:
            continue
        for pattern in ENDPOINT_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            candidate = match.group("endpoint").rstrip(".,;:")
            # Only a path counts as a route. Without this guard the default summary
            # "5xx responses on order-tracker" yields the service name.
            if candidate.startswith("/"):
                return candidate
    return None


def _window(alert: dict, now: datetime, fallback_seconds: int):
    starts_at = _parse_timestamp(alert.get("startsAt"))
    if starts_at is None:
        starts_at = now - timedelta(seconds=fallback_seconds)
    ends_at = _parse_timestamp(alert.get("endsAt"))
    if ends_at is not None and ends_at <= MIN_TIMESTAMP:
        ends_at = None
    if ends_at is None or ends_at < starts_at:
        ends_at = now
    return starts_at, ends_at, max(1, int((ends_at - starts_at).total_seconds()))


def build_context(alert: dict, now: datetime, fallback_seconds: int = 900) -> AlertContext:
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    annotations = (
        alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
    )
    values = alert.get("values") if isinstance(alert.get("values"), dict) else {}

    alertname = str(labels.get("alertname") or annotations.get("summary") or "unknown")
    severity = str(labels.get("severity") or "none")
    status = str(alert.get("status") or FIRING).strip().lower() or FIRING
    description = _text_field(annotations, DESCRIPTION_KEYS)
    summary = _text_field(annotations, SUMMARY_KEYS, default=alertname)
    starts_at, ends_at, window_seconds = _window(alert, now, fallback_seconds)

    return AlertContext(
        fingerprint=alert_fingerprint(alert, labels),
        alertname=alertname,
        severity=severity,
        status=status,
        endpoint=alert_endpoint(labels, summary, description),
        summary=summary,
        description=description,
        starts_at=starts_at,
        ends_at=ends_at,
        window_seconds=window_seconds,
        labels=dict(labels),
        annotations=dict(annotations),
        values=dict(values),
        generator_url=str(alert.get("generatorURL") or ""),
    )


def parse_webhook(body: bytes, now: datetime, fallback_seconds: int = 900) -> list[AlertContext]:
    if not body:
        raise PayloadError("Empty request body")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PayloadError("Body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PayloadError("Body must be a JSON object")

    alerts = payload.get("alerts")
    if alerts is None:
        raise PayloadError("Payload is missing the 'alerts' array")
    if not isinstance(alerts, list):
        raise PayloadError("'alerts' must be an array")

    contexts = []
    for alert in alerts:
        if not isinstance(alert, dict):
            raise PayloadError("Each entry in 'alerts' must be an object")
        contexts.append(build_context(alert, now, fallback_seconds))
    return contexts
