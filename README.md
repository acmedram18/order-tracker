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

`docker compose up` starts five services. Only the app port is needed to use the app; the rest are the telemetry backend.

| Service | URL | Role |
| --- | --- | --- |
| `app` | <http://127.0.0.1:8000> | The order tracker, instrumented |
| `responder` | <http://127.0.0.1:8001> | Incident responder, receives Alertmanager webhooks |
| `prometheus` | <http://127.0.0.1:9090> | Scrapes `/metrics`, evaluates alert rules |
| `alertmanager` | <http://127.0.0.1:9093> | Groups and routes alerts to the responder |
| `jaeger` | <http://127.0.0.1:16686> | Trace UI (OTLP gRPC on 4317, internal only) |

Each port can be moved with `ORDER_TRACKER_PORT`, `ORDER_TRACKER_RESPONDER_PORT`, `PROMETHEUS_PORT`, `ALERTMANAGER_PORT`, and `JAEGER_PORT`.

### Metrics

The app serves Prometheus metrics on `/metrics` via `prometheus-fastapi-instrumentator`. Alongside the standard HTTP metrics (`http_requests_total`, `http_request_duration_seconds`) it exports three domain metrics:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `orders_created_total` | counter | `priority` | Orders created |
| `order_status_changes_total` | counter | `status` | Status transitions |
| `express_orders_overdue` | gauge | none | Express orders past their estimated delivery |

`express_orders_overdue` is recomputed on startup and after every create and status change. Only bounded values such as `priority` and `status` are used as labels, never customer names or order IDs.

### Traces and logs

Requests are traced with OpenTelemetry and exported over OTLP gRPC to Jaeger. Logs are structured JSON via `structlog` and carry the `trace_id` and `span_id` of the active span, so a log line can be joined to its trace in Jaeger:

```bash
docker compose logs app | grep order_created
docker compose logs app | jq -r 'select(.event == "order_created") | .trace_id'
```

Set `OTEL_SDK_DISABLED=true` to turn tracing off. The test suite sets it so tests do not wait on a collector that is not running.

## Alerts

Rules live in `prometheus/rules.yml` and are evaluated every 15 seconds.

| Alert | Severity | Fires when |
| --- | --- | --- |
| `OrderTrackerDown` | critical | Prometheus cannot scrape the app |
| `OrderTrackerHighErrorRate` | critical | 5xx ratio over 5m exceeds 5%, with enough traffic |
| `OrderTrackerSlowResponses` | warning | p95 latency over 5m exceeds 1s |
| `ExpressOrdersOverdue` | warning | `express_orders_overdue > 0` |

`ExpressOrdersOverdue` is a business alert rather than a technical one. It is firing on a fresh stack by design: the seeded order `express-1002` is an express order created on the last day of the previous month that is still `preparing`, so it is already past its two-day estimate. That gives you a real incident to react to without having to break anything first.

Check what is firing:

```bash
curl -s http://127.0.0.1:9090/api/v1/alerts
```

`OrderTrackerDown` inhibits the other two technical alerts, so a dead target does not produce an alert storm.

## Incident responder

The responder is a small service that turns each distinct alert into a tracked incident. It receives Alertmanager webhooks on `POST /alerts`, deduplicates them by alert fingerprint, and stores one incident per fingerprint.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/alerts` | Alertmanager webhook receiver |
| GET | `/incidents` | List incidents, newest first |
| GET | `/incidents/{id}` | One incident |
| GET | `/healthz` | Database health check |

```bash
curl -s http://127.0.0.1:8001/incidents
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
curl -s http://127.0.0.1:8001/incidents
docker compose start app
```

Expect about 90 seconds from stopping the app to an open incident: 15 seconds to notice the failed scrape, `for: 1m` before the alert fires, then Alertmanager's `group_wait`.

## Tests

```bash
uv run --frozen pytest -q
```

`tests/test_telemetry.py` covers the metrics endpoint, the domain counters, the overdue gauge, and the delivery estimate. `tests/test_incident.py` covers webhook parsing, deduplication, reopen-after-resolve, the notification path, and the concurrency guarantees.

## Notes on the delivery estimate

Express orders are promised two days after they are placed. That estimate is computed with `timedelta` arithmetic in `estimated_delivery()`, which crosses month and year boundaries correctly. An earlier version used `placed_at.replace(day=placed_at.day + 2)`, which raises `ValueError` for any order placed on the 29th or later of a month, and turned `GET /api/orders/{id}` into a `500` for the seeded month-end order.