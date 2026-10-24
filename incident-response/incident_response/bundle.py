"""Renders the human- and agent-readable incident summary."""

import json
import urllib.parse
from datetime import datetime, timezone

MAX_LOG_ROWS = 40
MAX_SPANS_PER_TRACE = 12


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _ms(moment: int) -> int:
    return moment * 1000


def grafana_link(settings, alert) -> str:
    params = urllib.parse.urlencode(
        {
            "from": _ms(int(alert.starts_at.timestamp())),
            "to": _ms(int((alert.ends_at or datetime.now(timezone.utc)).timestamp())),
            "var-handler": alert.endpoint or "",
        }
    )
    return f"{settings.grafana_url}/d/{settings.dashboard_uid}/?{params}"


def loki_link(settings, alert) -> str:
    # No line filter: the log line is the event name (``order_lookup``), and the
    # route lives in structured metadata, which LogQL cannot filter on anyway.
    expr = f'{{service_name="{settings.service_name}"}}'
    left = {
        "datasource": "order-tracker-loki",
        "queries": [{"expr": expr, "refId": "A"}],
        "range": {
            "from": _ms(int(alert.starts_at.timestamp())),
            "to": _ms(int((alert.ends_at or datetime.now(timezone.utc)).timestamp())),
        },
    }
    return f"{settings.loki_url}/explore?left={urllib.parse.quote(json.dumps(left))}"


def tempo_link(settings, trace_id: str) -> str:
    return f"{settings.tempo_url}/trace/{trace_id}"


def render(settings, alert, evidence, incident_id: str, generated_at: str | None = None) -> str:
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    out: list[str] = []
    add = out.append

    add(f"# Incident {incident_id}")
    add("")
    add(f"- **Alert**: {alert.alertname}")
    add(f"- **Severity**: {alert.severity}")
    add(f"- **State**: {alert.status}")
    add(f"- **Fingerprint**: `{alert.fingerprint}`")
    add(f"- **Affected endpoint**: `{alert.endpoint or 'unknown'}`")
    add(
        f"- **Time window**: {alert.starts_at.isoformat()} "
        f"-> {(alert.ends_at or datetime.now(timezone.utc)).isoformat()} "
        f"({alert.window_seconds}s)"
    )
    add(f"- **Collected at**: {generated_at}")
    add("")

    if alert.summary:
        add("## Summary")
        add("")
        add(alert.summary)
        add("")

    add("## Metrics")
    add("")
    metrics = evidence.metrics or {}
    if metrics:
        add(f"- **5xx in window**: {_fmt(metrics.get('errors_5xx'))}")
        add(f"- **Request rate**: {_fmt(metrics.get('request_rate'))}")
        by_endpoint = metrics.get("errors_by_endpoint")
        if isinstance(by_endpoint, list) and by_endpoint:
            add("- **5xx by endpoint**:")
            for item in by_endpoint:
                add(f"  - `{item.get('handler')}`: {_fmt(item.get('value'))}")
    else:
        add("_none collected_")
    add("")

    add("## Logs")
    add("")
    if evidence.logs:
        add("| time | level | status | route | trace_id | message |")
        add("| --- | --- | --- | --- | --- | --- |")
        for entry in evidence.logs[:MAX_LOG_ROWS]:
            add(
                f"| {entry.timestamp} | {entry.level or '-'} | {entry.status or '-'} "
                f"| `{entry.route or '-'}` | `{entry.trace_id or '-'}` | {entry.line} |"
            )
        if len(evidence.logs) > MAX_LOG_ROWS:
            add(f"| _... {len(evidence.logs) - MAX_LOG_ROWS} more in logs.json_ | | | | | |")
    else:
        add("_no logs matched in the window_")
    add("")

    add("## Traces")
    add("")
    if evidence.traces:
        for trace in evidence.traces:
            head = f"### `{trace.trace_id}`"
            if trace.root:
                head += f" - {trace.root}"
            if trace.duration_ms is not None:
                head += f" - {_fmt(trace.duration_ms)}ms"
            add(head)
            add("")
            if trace.error:
                add(f"_tempo: {trace.error}_")
                add("")
                continue
            add(f"[Open in Tempo]({tempo_link(settings, trace.trace_id)})")
            add("")
            add("| span | duration_ms | status |")
            add("| --- | --- | --- |")
            for span in trace.spans[:MAX_SPANS_PER_TRACE]:
                add(f"| `{span.name}` | {_fmt(span.duration_ms)} | {span.status or '-'} |")
            add("")
    else:
        add("_no traces found for this window_")
    add("")

    add("## Where to look")
    add("")
    add(f"- [Dashboard]({grafana_link(settings, alert)})")
    add(f"- [Logs in Loki]({loki_link(settings, alert)})")
    add("")

    if evidence.errors:
        add("## Collection problems")
        add("")
        for problem in evidence.errors:
            add(f"- {problem}")
        add("")

    add("## Investigation notes")
    add("")
    add("_The coding assistant should record findings here._")
    add("")
    return "\n".join(out)
