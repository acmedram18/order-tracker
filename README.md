# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup, plus telemetry, alert rules, and an incident responder.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/metrics` | Prometheus metrics |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.

## Observability stack

`docker compose up` starts eight services. Only the app port is needed to use the app; the rest are the telemetry backend.

| Service | URL | Role |
| --- | --- | --- |
| `app` | <http://127.0.0.1:8000> | The order tracker, instrumented |
| `responder` | <http://127.0.0.1:8002> | Incident responder, receives Alertmanager webhooks |
| `collector` | OTLP <http://127.0.0.1:4318> | Single OTLP gateway for all signals |
| `prometheus` | <http://127.0.0.1:9090> | Scrapes the collector, evaluates alert rules |
| `loki` | <http://127.0.0.1:3100> | Log store and query API |
| `tempo` | <http://127.0.0.1:3200> | Trace store and query API |
| `grafana` | <http://127.0.0.1:3000> | Dashboards (anonymous view, `admin`/`admin`) |
| `alertmanager` | <http://127.0.0.1:9093> | Groups and routes alerts to the responder |

### Signal flow

The collector is the only path into the backends. Prometheus does not scrape the app directly:

```
app  --OTLP logs----> collector --OTLP/HTTP--> loki
app  --OTLP traces--> collector --OTLP/gRPC --> tempo
app  --/metrics-----> collector --:8889-------> prometheus -> grafana
```

Prometheus scrapes the collector's `:8889` exporter, which is fed by the collector's
`prometheus` receiver. The collector re-exports the app's `job="order-tracker"` label, so
alert rules match the same series they did when Prometheus scraped the app directly.

Grafana provisions three datasources and one dashboard, *Order Tracker - Requests and
Errors*, with request rate, error rate, per-route traffic, latency percentiles, and recent
warning and error logs pulled from Loki.

Each port can be moved with `ORDER_TRACKER_PORT`, `ORDER_TRACKER_RESPONDER_PORT`, `PROMETHEUS_PORT`, `ALERTMANAGER_PORT`, `LOKI_PORT`, `TEMPO_PORT`, and `GRAFANA_PORT`.

### Metrics

The app serves Prometheus metrics on `/metrics` via `prometheus-fastapi-instrumentator`. Alongside the standard HTTP metrics (`http_requests_total`, `http_request_duration_seconds`) it exports three domain metrics:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `orders_created_total` | counter | `priority` | Orders created |
| `order_status_changes_total` | counter | `status` | Status transitions |
| `express_orders_overdue` | gauge | none | Express orders past their estimated delivery |

`express_orders_overdue` is recomputed on startup and after every create and status change. Only bounded values such as `priority` and `status` are used as labels, never customer names or order IDs.

### Traces and logs

Requests are traced with OpenTelemetry and exported over OTLP gRPC to the collector. Each order lookup also opens an `order.lookup` child span carrying `http.route` and `http.status_code`. Logs are structured JSON via `structlog` and are exported over OTLP to the collector as well; the collector forwards them to Loki, which promotes `trace_id`, `span_id`, `severity_text`, `route`, and `http_status_code` to indexed labels.

That means a log line joins to its trace in Tempo without any manual correlation:

```bash
docker compose logs app | grep order_created
docker compose logs app | jq -r 'select(.event == "order_created") | .trace_id'
```

Query the same trace straight out of Loki:

```bash
curl -s -G http://127.0.0.1:3100/loki/api/v1/query_range \
  --data-urlencode 'query={service_name="order-tracker"}' | jq '.data.result[].stream.trace_id'
```

Set `OTEL_SDK_DISABLED=true` to turn tracing off. The test suite sets it so tests do not wait on a collector that is not running. Set `CONSOLE_SIGNALS_ENABLED=true` to also print spans and OTel metrics to stdout for local debugging; it is off by default now that the real pipeline is in place.

## Alerts

Rules live in `prometheus/rules.yml` and are evaluated every 15 seconds.

| Alert | Severity | Fires when |
| --- | --- | --- |
| `OrderTrackerDown` | critical | The collector is not exporting app metrics |
| `OrderTrackerHighErrorRate` | critical | 5xx ratio over 5m exceeds 5%, with enough traffic |
| `OrderTrackerSlowResponses` | warning | p95 latency over 5m exceeds 1s |
| `ExpressOrdersOverdue` | warning | `express_orders_overdue > 0` |

`ExpressOrdersOverdue` is a business alert rather than a technical one. It is firing on a fresh stack by design: the seeded order `express-1002` is an express order created on the last day of the previous month that is still `preparing`, so it is already past its two-day estimate. That gives you a real incident to react to without having to break anything first.

Check what is firing:

```bash
curl -s http://127.0.0.1:9090/api/v1/alerts
```

`OrderTrackerDown` inhibits the other two technical alerts, so a dead target does not produce an alert storm.

### Grafana alert: 5xx responses

A second, independent 5xx check runs inside Grafana rather than Prometheus, provisioned from `grafana/provisioning/alerting/order-tracker-5xx.yaml` and evaluated every 1 minute over a 5 minute rolling window. Find it under **Alerting → Alert rules → Order Tracker**.

It raises one alert instance per failing endpoint, and each one names the endpoint, the window, and the dashboard:

```
summary: 5xx responses on /api/orders/{order_id} in order-tracker over the
         last 5 minutes (5m rolling window, evaluated every 1m).
```

The endpoint comes from a `handler` label carried through the expression into `{{ $labels.handler }}`, and the alert body carries `dashboardUid` plus `panelId`, so Grafana renders a direct link to the *5xx errors per second* panel.

Periods with no 5xx are handled explicitly rather than by accident:

- The threshold query is `sum by (handler) (increase(http_requests_total{status=~"5.."}[5m])) or vector(0)`. Without the `or vector(0)` fallback, `sum()` over a set with no matching series returns an *empty vector*, which Grafana reports as `NoData` rather than `OK`.
- `noDataState: OK` is set as a second line of defence, so a quiet period always reads as healthy instead of raising an unresolved alert.

One characteristic of running the collector as a scrape gateway: `increase()` needs two samples to compute a delta, so immediately after a restart of the app or the collector the first window can read `0` even if errors occurred. The alert arms on the next evaluation once Prometheus has a baseline.

## Incident responder

The responder is a small service that turns each distinct alert into a tracked incident. It receives Alertmanager webhooks on `POST /alerts`, deduplicates them by alert fingerprint, and stores one incident per fingerprint.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/alerts` | Alertmanager webhook receiver |
| GET | `/incidents` | List incidents, newest first |
| GET | `/incidents/{id}` | One incident |
| GET | `/healthz` | Database health check |

It listens on host port `8002` so that the host-side [incident response
service](#incident-response-service) can own `8001`. The port is set through
`.env`; inside the compose network it is still `8001`, which is what
Alertmanager uses (`http://responder:8001/alerts`).

```bash
curl -s http://127.0.0.1:8002/incidents
```

Behaviour worth knowing:

- An alert that keeps firing does not create a new incident. It increments `notification_count` on the existing one.
- A resolved alert marks the incident `resolved` and sets `resolved_at`. Alertmanager sends resolutions because `send_resolved` is on.
- If the same alert fires again later, the existing incident is reopened: status goes back to `firing` and `resolved_at` is cleared. Fingerprints are held under a `UNIQUE` index, so this is enforced by the database and not by timing.
- A malformed body returns `400`. An unexpected internal error is logged and answered with `200`, because a `5xx` would make Alertmanager retry forever.
- `POST /alerts` does its SQLite and notification work in a threadpool, so a slow notification target cannot stall the service.

To have the responder page something, point it at a webhook:

```bash
INCIDENT_WEBHOOK_URL=https://example.com/hooks/incidents docker compose up -d responder
```

When it is unset, incidents are recorded but no outbound notification is sent. A failed notification is logged and never blocks the webhook response.

### Trying it end to end

Watch an incident open and close on its own. Resolving the overdue order drops the gauge, Prometheus resolves the alert, and the responder closes the incident:

```bash
curl -s -X PATCH http://127.0.0.1:8000/api/orders/express-1002 \
  -H 'content-type: application/json' -d '{"status":"delivered"}'
curl -s http://127.0.0.1:8000/metrics | grep express_orders_overdue
```

Or take the app down and watch `OrderTrackerDown` fire, roughly 90 seconds later:

```bash
docker compose stop app
curl -s http://127.0.0.1:8002/incidents
docker compose start app
```

Expect about 90 seconds from stopping the app to an open incident: 15 seconds to notice the failed scrape, `for: 1m` before the alert fires, then Alertmanager's `group_wait`.

## Incident response service

The responder above tracks incidents. This one investigates them: it receives the
Grafana 5xx alert, gathers the logs and traces for the failing endpoint, writes a
bundle, and hands that bundle to `opencode` running headlessly.

It lives in [`incident-response/`](incident-response) and runs on the host, because
`opencode` needs its own ambient credentials and must edit the repo in place.

```bash
cd incident-response
uv sync
uv run uvicorn incident_response.main:app --host 0.0.0.0 --port 8001
```

On each firing alert it writes `data/incidents/<timestamp>-<fingerprint>-<id>/`
containing `alert.json`, `metrics.json`, `logs.json`, `traces.json`, `bundle.md`
and `agent.log`, then runs:

```bash
opencode run --auto --format json "<prompt>" -f bundle.md -f logs.json -f traces.json
```

The agent is told to form a hypothesis with evidence before it edits anything.
Set `AGENT_ENABLED=false` to collect the evidence without spending on a model.

Grafana is not wired to it yet. When it is, the contact point URL is
`http://host.docker.internal:8001/alerts`, which works because the service binds
`0.0.0.0`; see [`incident-response/README.md`](incident-response/README.md) for
the evidence-collection details and the full configuration.

## Tests

```bash
uv run --frozen pytest -q
```

`tests/test_telemetry.py` covers the metrics endpoint, the domain counters, the overdue gauge, and the delivery estimate. `tests/test_incident.py` covers webhook parsing, deduplication, reopen-after-resolve, the notification path, and the concurrency guarantees.

The incident response service has its own suite in
[`incident-response/tests/`](incident-response/tests), run with
`uv run pytest` from that directory.

## Notes on the delivery estimate

Express orders are promised two days after they are placed. That estimate is computed with `timedelta` arithmetic in `estimated_delivery()`, which crosses month and year boundaries correctly. An earlier version used `placed_at.replace(day=placed_at.day + 2)`, which raises `ValueError` for any order placed on the 29th or later of a month, and turned `GET /api/orders/{id}` into a `500` for the seeded month-end order.