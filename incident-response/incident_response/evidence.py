"""Collects the evidence an investigator needs: Loki logs, Tempo traces, Prometheus counts.

Two constraints found while building this shape the code:

* Loki only indexes ``service_name`` and ``deployment_environment``. ``route``,
  ``trace_id`` and ``http_status_code`` arrive as structured metadata, so a
  stream selector like ``route="/api/orders/{order_id}"`` matches nothing. Logs
  are therefore fetched by service and filtered here in Python.
* Tempo's search API returns nothing when filtering on ``http.route``, so traces
  are fetched by trace id (taken from the logs) and only fall back to search.

Every backend call is best effort: failures are collected in ``Evidence.errors``
so a partial bundle is still written.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

USER_AGENT = "order-tracker-incident-response/1.0"


@dataclass
class LogEntry:
    timestamp: str
    line: str
    route: str | None = None
    level: str | None = None
    status: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    labels: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "line": self.line,
            "route": self.route,
            "level": self.level,
            "status": self.status,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "labels": self.labels,
        }


@dataclass
class Span:
    name: str
    duration_ms: float
    trace_id: str = ""
    span_id: str = ""
    status: str = ""
    attributes: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "duration_ms": self.duration_ms,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "status": self.status,
            "attributes": self.attributes,
        }


@dataclass
class Trace:
    trace_id: str
    spans: list = field(default_factory=list)
    service: str | None = None
    root: str | None = None
    duration_ms: float | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "service": self.service,
            "root": self.root,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "spans": [span.as_dict() for span in self.spans],
        }


@dataclass
class Evidence:
    endpoint: str | None = None
    window_start: str = ""
    window_end: str = ""
    logs: list = field(default_factory=list)
    traces: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def trace_ids(self) -> list[str]:
        seen = []
        for entry in self.logs:
            if entry.trace_id and entry.trace_id not in seen:
                seen.append(entry.trace_id)
        return seen

    def as_dict(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "log_count": len(self.logs),
            "trace_count": len(self.traces),
            "metrics": self.metrics,
            "errors": self.errors,
            "logs": [entry.as_dict() for entry in self.logs],
            "traces": [trace.as_dict() for trace in self.traces],
        }


def get_json(url: str, params: dict, timeout: float) -> dict:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    full = f"{url}?{query}" if query else url
    request = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _ns(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000_000)


def _attribute_value(raw) -> object:
    if isinstance(raw, dict):
        for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if key in raw:
                return raw[key]
        return json.dumps(raw, sort_keys=True)
    return raw


def _attributes(items) -> dict:
    out = {}
    for item in items or []:
        if isinstance(item, dict) and "key" in item:
            out[item["key"]] = _attribute_value(item.get("value"))
    return out


def collect_logs(loki_url, service_name, start, end, endpoint, limit, timeout):
    """Fetch logs for the service in the window, then narrow to the endpoint.

    The endpoint filter is applied here rather than in LogQL because ``route`` is
    structured metadata, not an indexed label. Not every route carries it though:
    observed against the live stack, ``/api/orders/{order_id}`` logs carry
    ``route`` while ``/api/orders`` logs have none. So route-attributed lines are
    preferred, and the unattributed lines are only used as a fallback when the
    endpoint produced no attributed lines of its own.
    """
    entries: list[LogEntry] = []
    unattributed: list[LogEntry] = []
    query = f'{{service_name="{service_name}"}}'
    payload = get_json(
        f"{loki_url}/loki/api/v1/query_range",
        {
            "query": query,
            "start": _ns(start),
            "end": _ns(end),
            "limit": limit,
            "direction": "backward",
        },
        timeout,
    )
    if payload.get("status") not in (None, "success"):
        raise ValueError(payload.get("error") or "Loki returned an error status")

    for stream in payload.get("data", {}).get("result", []) or []:
        labels = stream.get("stream") or {}
        route = labels.get("route")
        for value in stream.get("values") or []:
            if len(value) < 2:
                continue
            raw_ts, line = value[0], value[1]
            try:
                moment = datetime.fromtimestamp(int(raw_ts) / 1_000_000_000, tz=timezone.utc)
            except (TypeError, ValueError):
                moment = None
            entry = LogEntry(
                timestamp=moment.isoformat() if moment else str(raw_ts),
                line=line,
                route=route,
                level=labels.get("severity_text") or labels.get("detected_level"),
                status=labels.get("http_status_code"),
                trace_id=labels.get("trace_id"),
                span_id=labels.get("span_id"),
                labels=labels,
            )
            if not endpoint or route == endpoint:
                entries.append(entry)
            else:
                unattributed.append(entry)

    if endpoint and not entries:
        entries = unattributed

    entries.sort(key=lambda item: item.timestamp, reverse=True)
    return entries[:limit]


def parse_trace(trace_id: str, payload: dict, span_limit: int) -> Trace:
    spans: list[Span] = []
    service = None
    root = None
    for batch in payload.get("batches", []) or []:
        resource = _attributes((batch.get("resource") or {}).get("attributes"))
        service = service or resource.get("service.name")
        for scope in batch.get("scopeSpans", []) or []:
            for span in scope.get("spans", []) or []:
                try:
                    start_ns = int(span.get("startTimeUnixNano", 0))
                    end_ns = int(span.get("endTimeUnixNano", 0))
                    duration_ms = round((end_ns - start_ns) / 1_000_000, 3)
                except (TypeError, ValueError):
                    duration_ms = 0.0
                name = span.get("name") or ""
                if root is None and not name.endswith("http send"):
                    root = name
                spans.append(
                    Span(
                        name=name,
                        duration_ms=duration_ms,
                        trace_id=span.get("traceId", ""),
                        span_id=span.get("spanId", ""),
                        status=str((span.get("status") or {}).get("code", "")),
                        attributes=_attributes(span.get("attributes")),
                    )
                )
    spans.sort(key=lambda item: item.duration_ms, reverse=True)
    total = sum(item.duration_ms for item in spans)
    return Trace(
        trace_id=trace_id,
        spans=spans[:span_limit],
        service=service,
        root=root,
        duration_ms=round(total, 3),
    )


def fetch_trace(tempo_url: str, trace_id: str, span_limit: int, timeout: float) -> Trace:
    try:
        payload = get_json(f"{tempo_url}/api/traces/{trace_id}", {}, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return Trace(trace_id=trace_id, error="not found in Tempo")
        raise
    if "batches" not in payload:
        return Trace(trace_id=trace_id, error="Tempo returned no batches")
    return parse_trace(trace_id, payload, span_limit)


def search_traces(tempo_url, service_name, start, end, limit, timeout):
    payload = get_json(
        f"{tempo_url}/api/search",
        {
            "tags": f"service.name={service_name}",
            "start": int(start.timestamp()),
            "end": int(end.timestamp()),
            "limit": limit,
        },
        timeout,
    )
    return payload.get("traces", []) or []


def collect_metrics(prometheus_url, endpoint, window_seconds, timeout) -> dict:
    window = f"{max(60, int(window_seconds))}s"
    selector = f'http_requests_total{{status=~"5.."{f",handler=\"{endpoint}\"" if endpoint else ""}}}'
    queries = {
        "errors_5xx": f"sum(increase({selector}[{window}])) or vector(0)",
        "errors_by_endpoint": (
            f'topk(5, sum by (handler) (increase({selector}[{window}])))'
        ),
        "request_rate": f"sum(rate(http_requests_total[{window}])) or vector(0)",
    }
    metrics: dict = {}
    for name, query in queries.items():
        try:
            payload = get_json(
                f"{prometheus_url}/api/v1/query", {"query": query}, timeout
            )
        except Exception as exc:  # noqa: BLE001 - metrics are best effort
            metrics[name] = {"error": str(exc)}
            continue
        result = payload.get("data", {}).get("result", []) or []
        if name == "errors_by_endpoint":
            metrics[name] = [
                {"handler": item["metric"].get("handler"), "value": item["value"][1]}
                for item in result
            ]
        else:
            metrics[name] = result[0]["value"][1] if result else "0"
    return metrics


def collect(settings, alert) -> Evidence:
    """Gather logs, traces and metrics for one alert, never raising."""
    evidence = Evidence(
        endpoint=alert.endpoint,
        window_start=alert.starts_at.isoformat(),
        window_end=alert.ends_at.isoformat() if alert.ends_at else "",
    )
    timeout = settings.http_timeout

    try:
        evidence.logs = collect_logs(
            settings.loki_url,
            settings.service_name,
            alert.starts_at,
            alert.ends_at or datetime.now(timezone.utc),
            alert.endpoint,
            settings.log_limit,
            timeout,
        )
    except Exception as exc:  # noqa: BLE001 - partial evidence is still useful
        evidence.errors.append(f"loki: {exc}")

    trace_ids = evidence.trace_ids()[: settings.trace_limit]
    for trace_id in trace_ids:
        try:
            evidence.traces.append(
                fetch_trace(settings.tempo_url, trace_id, settings.span_limit, timeout)
            )
        except Exception as exc:  # noqa: BLE001
            evidence.errors.append(f"tempo {trace_id}: {exc}")

    if not evidence.traces:
        try:
            found = search_traces(
                settings.tempo_url,
                settings.service_name,
                alert.starts_at,
                alert.ends_at or datetime.now(timezone.utc),
                settings.trace_limit,
                timeout,
            )
            for item in found[: settings.trace_limit]:
                candidate = item.get("traceID")
                if not candidate:
                    continue
                try:
                    evidence.traces.append(
                        fetch_trace(settings.tempo_url, candidate, settings.span_limit, timeout)
                    )
                except Exception as exc:  # noqa: BLE001
                    evidence.errors.append(f"tempo {candidate}: {exc}")
        except Exception as exc:  # noqa: BLE001
            evidence.errors.append(f"tempo search: {exc}")

    try:
        evidence.metrics = collect_metrics(
            settings.prometheus_url, alert.endpoint, alert.window_seconds, timeout
        )
    except Exception as exc:  # noqa: BLE001
        evidence.errors.append(f"prometheus: {exc}")

    return evidence
