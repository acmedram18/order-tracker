"""HTTP service: Grafana alerts in, evidence bundle plus agent run out."""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import structlog
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from . import agent as agent_module
from . import bundle as bundle_module
from . import evidence as evidence_module
from . import store as store_module
from .config import load_settings
from .payload import PayloadError, parse_webhook

settings = load_settings()
store = store_module.Store(settings.index_db, settings.bundle_root())
runner = agent_module.AgentRunner(settings)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ]
    )


logger = structlog.get_logger("incident_response")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    orphans = store.mark_running_agents_interrupted()
    logger.info(
        "incident_response_started",
        db_path=str(settings.index_db),
        bundle_root=str(settings.bundle_root()),
        agent_enabled=settings.agent_enabled,
        interrupted_agent_runs=orphans,
    )
    yield


app = FastAPI(title="Incident Response", lifespan=lifespan)


def empty_counts(size: int) -> dict:
    return {"received": size, "opened": 0, "updated": 0, "resolved": 0}


def investigate(incident_id: str, alert, fingerprint: str) -> None:
    """Collect evidence, write the bundle, then launch the assistant."""
    try:
        found = evidence_module.collect(settings, alert)
        logger.info(
            "evidence_collected",
            incident_id=incident_id,
            logs=len(found.logs),
            traces=len(found.traces),
            errors=found.errors,
        )
        created = alert.starts_at.isoformat()
        directory = store.bundle_dir(incident_id, fingerprint, created)
        markdown = bundle_module.render(settings, alert, found, incident_id)
        files = store.write_bundle(directory, alert, found, incident_id, markdown)
        store.set_bundle_path(incident_id, directory)
        logger.info("bundle_written", incident_id=incident_id, path=str(directory))

        if not runner.is_running(fingerprint):
            result = runner.run(alert, found, files, store, incident_id)
            log = {
                "event": "agent_launched",
                "incident_id": incident_id,
                "status": result["status"],
                "session_id": result.get("session_id"),
                "duration_s": result.get("duration_s"),
                "cost": result.get("cost"),
                "has_answer": result.get("has_answer"),
            }
            if result.get("error"):
                log["error"] = result["error"]
            logger.info(**log)
            if not result.get("has_answer", True):
                logger.warning("agent produced no usable answer", **log)
            else:
                logger.info("agent_answer", incident_id=incident_id, answer=result.get("output"))
        else:
            logger.info("agent_skipped_running", incident_id=incident_id)
    except Exception:  # noqa: BLE001 - background work must not kill the service
        logger.exception("investigation_failed", incident_id=incident_id)


@app.post("/alerts", status_code=200)
async def receive_alerts(request: Request, background: BackgroundTasks):
    try:
        alerts = parse_webhook(
            await request.body(),
            datetime.now(timezone.utc),
            settings.window_fallback_seconds,
        )
    except PayloadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    counts = empty_counts(len(alerts))
    timestamp = store_module.now_iso()
    for alert in alerts:
        if alert.resolved:
            resolved = store.resolve_incident(alert.fingerprint, timestamp)
            counts["resolved"] += 1
            if resolved:
                logger.info(
                    "incident_resolved", incident_id=resolved["id"], fingerprint=alert.fingerprint
                )
            continue

        created, incident = store.open_incident(alert, timestamp)
        counts["opened" if created else "updated"] += 1
        logger.info(
            "incident_opened" if created else "incident_updated",
            incident_id=incident["id"],
            fingerprint=alert.fingerprint,
            endpoint=alert.endpoint,
        )
        background.add_task(investigate, incident["id"], alert, alert.fingerprint)
    return counts


@app.get("/incidents")
async def list_incidents():
    return store.list_incidents()


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: str):
    incident = store.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident_not_found")
    return incident


@app.get("/healthz")
async def healthz():
    with store.connect() as db:
        db.execute("SELECT 1")
    return {
        "status": "ok",
        "agent_enabled": settings.agent_enabled,
        "agent_command": settings.agent_command,
        "loki_url": settings.loki_url,
        "tempo_url": settings.tempo_url,
    }


configure_logging()
