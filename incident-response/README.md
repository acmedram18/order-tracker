# Incident response service

A host-side service that turns a Grafana alert into an evidence bundle and then
lets `opencode` investigate it headlessly.

It runs on the **host**, not in Docker Compose, for two reasons:

- `opencode` needs its own ambient credentials, which live in
  `~/.local/share/opencode/opencode.db`. Mounting that into a container would be
  the wrong trade.
- It needs to read and edit the repo in place, which is awkward from a container.

## Port layout

| Service | Host port | In-network port |
| --- | --- | --- |
| This service | `8001` | n/a, host only |
| `responder` (existing, in Compose) | `8002` | `8001` |

Alertmanager keeps talking to `http://responder:8001/alerts`, which is unchanged.
Grafana reaches this service at `http://host.docker.internal:8001`.

## Run it

```bash
cd incident-response
uv sync
uv run uvicorn incident_response.main:app --host 0.0.0.0 --port 8001
```

`--host 0.0.0.0` matters: Grafana runs in a container and can only reach a
host-bound port through `host.docker.internal`.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/alerts` | Grafana unified webhook receiver |
| GET | `/incidents` | List incidents, newest first |
| GET | `/incidents/{id}` | One incident, with agent status |
| GET | `/healthz` | Liveness plus resolved backend URLs |

The same parser accepts Alertmanager v4 payloads, since both send a top-level
`alerts` array. Unknown fields are ignored, because both senders add keys
between versions and a monitoring endpoint should not start returning `400`.

## What happens on an alert

1. **Deduplicate** by fingerprint. A repeated alert increments
   `notification_count`; a resolved alert closes the incident and a later refire
   reopens it.
2. **Collect evidence** for the alert window, best effort. A failing backend is
   recorded in `errors` and never fails the request.
3. **Write a bundle** under `data/incidents/<timestamp>-<fingerprint>-<id>/`.
4. **Launch `opencode`** headlessly, unless one run is already in flight.

The webhook returns `200` as soon as the incident is recorded; collection and the
agent run in a FastAPI background task.

## Bundle contents

| File | Contents |
| --- | --- |
| `alert.json` | The parsed alert, plus the raw payload |
| `metrics.json` | 5xx count and request rate from Prometheus |
| `logs.json` | Log lines from Loki, newest first |
| `traces.json` | Traces from Tempo with their spans |
| `bundle.md` | The human-readable summary the agent reads first |
| `agent.log` | The full `opencode` invocation and its output |

## Two things that are not obvious

**`route` is structured metadata in Loki, not an indexed label.** LogQL cannot
filter on it, so the query is `{service_name="order-tracker"}` and the endpoint
filter runs in Python afterwards. On top of that, not every route emits `route`
at all: measured on this stack, `/api/orders/{order_id}` logs carry it while
`/api/orders` logs do not. So route-attributed lines are preferred, and the
unattributed lines are used only when the endpoint produced no attributed lines
of its own. Filtering strictly would return nothing for `/api/orders`; filtering
loosely would drag in every unrelated request.

**Trace correlation goes through the logs.** Loki carries `trace_id` on every
line, so logs are fetched first and each distinct `trace_id` is looked up at
`GET /api/traces/{trace_id}`. Searching Tempo by `http.route` returns nothing on
this stack, so it is only a fallback.

## Agent invocation

```
opencode run --auto --format json "<prompt>" -f bundle.md -f logs.json -f traces.json
```

- `--auto` is required. Without it the run stalls on an approval prompt, which is
  useless headlessly.
- **The prompt must come before `-f`.** `-f` is a greedy array option and will
  otherwise swallow the prompt and treat it as a filename.
- `--dir` pins the run to the repo, and the prompt asks for a hypothesis with
  evidence *before* any edit.

The agent gets at most `AGENT_MAX_CONCURRENT` runs at a time; a second alert that
arrives while one is running is recorded as `busy` rather than queued.

Stopping the service does not stop the `opencode` child it spawned, and it is not
reaped on the way out. On the next startup any incident still marked `running` is
flipped to `interrupted`, so a killed investigation cannot look like it is still
in progress forever. Kill the strays yourself if that matters:

```bash
pkill -f "opencode run"
```

## Configuration

All optional, read from the environment.

| Variable | Default | Purpose |
| --- | --- | --- |
| `INCIDENT_DATA_DIR` | `../data/incidents` | Bundle output root |
| `INCIDENT_INDEX_DB` | `../data/incidents.db` | SQLite index |
| `INCIDENT_LOKI_URL` | `http://127.0.0.1:3100` | Loki |
| `INCIDENT_TEMPO_URL` | `http://127.0.0.1:3200` | Tempo |
| `INCIDENT_PROM_URL` | `http://127.0.0.1:9090` | Prometheus |
| `INCIDENT_SERVICE_NAME` | `order-tracker` | Loki stream selector |
| `INCIDENT_WINDOW_FALLBACK` | `900` | Window when `startsAt` is missing |
| `INCIDENT_LOG_LIMIT` | `200` | Max log lines per bundle |
| `INCIDENT_TRACE_LIMIT` | `5` | Max traces per bundle |
| `INCIDENT_SPAN_LIMIT` | `60` | Max spans kept per trace |
| `AGENT_ENABLED` | `true` | Set `false` to record evidence without launching |
| `AGENT_COMMAND` | `opencode` | Must be on `PATH` |
| `AGENT_MODEL` | unset | Model passed as `--model` |
| `AGENT_DIR` | repo root | `--dir` for the run |
| `AGENT_TIMEOUT` | `600` | Seconds before the run is killed |
| `AGENT_MAX_CONCURRENT` | `1` | Concurrent runs |

Set `AGENT_ENABLED=false` to collect evidence without spending on a model. If the
binary is missing the run is recorded as `skipped`, not failed.

## Tests

```bash
uv run pytest
```

60 tests, no network. Backends are stubbed and the agent subprocess is replaced,
so no test can spawn a real `opencode` session or touch Loki, Tempo or
Prometheus.
