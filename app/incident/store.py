import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4


DB_PATH = Path(os.getenv("INCIDENT_DB_PATH", "data/incidents.db"))
STATUS_FIRING = "firing"
STATUS_RESOLVED = "resolved"


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                alertname TEXT NOT NULL,
                severity TEXT NOT NULL,
                summary TEXT NOT NULL,
                status TEXT NOT NULL,
                labels TEXT NOT NULL,
                notification_count INTEGER NOT NULL DEFAULT 1,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                resolved_at TEXT,
                notified INTEGER NOT NULL DEFAULT 0
            )"""
        )
        dedupe_incidents(db)
        db.execute("DROP INDEX IF EXISTS incidents_fingerprint_idx")
        db.execute(
            "CREATE UNIQUE INDEX incidents_fingerprint_idx ON incidents (fingerprint)"
        )


def dedupe_incidents(db):
    db.execute(
        "DELETE FROM incidents WHERE rowid NOT IN"
        " (SELECT MIN(rowid) FROM incidents GROUP BY fingerprint)"
    )


def as_dict(row):
    return dict(row) if row else None


def find_by_fingerprint(fingerprint):
    with connect() as db:
        row = db.execute(
            "SELECT * FROM incidents WHERE fingerprint = ?"
            " ORDER BY first_seen DESC LIMIT 1",
            (fingerprint,),
        ).fetchone()
    return as_dict(row)


def insert_incident(fingerprint, alertname, severity, summary, labels, now):
    incident_id = str(uuid4())
    try:
        with connect() as db:
            db.execute(
                "INSERT INTO incidents"
                " (id, fingerprint, alertname, severity, summary, status, labels,"
                " notification_count, first_seen, last_seen, resolved_at, notified)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    incident_id,
                    fingerprint,
                    alertname,
                    severity,
                    summary,
                    STATUS_FIRING,
                    json.dumps(labels, sort_keys=True),
                    1,
                    now,
                    now,
                    None,
                    0,
                ),
            )
    except sqlite3.IntegrityError:
        return None
    return get_incident(incident_id)


def update_incident(incident, alertname, severity, summary, labels, now):
    with connect() as db:
        db.execute(
            "UPDATE incidents SET alertname = ?, severity = ?, summary = ?,"
            " status = ?, labels = ?, notification_count = notification_count + 1,"
            " last_seen = ?, resolved_at = NULL WHERE id = ?",
            (
                alertname,
                severity,
                summary,
                STATUS_FIRING,
                json.dumps(labels, sort_keys=True),
                now,
                incident["id"],
            ),
        )
    return get_incident(incident["id"])


def open_incident(fingerprint, alertname, severity, summary, labels, now):
    incident = insert_incident(fingerprint, alertname, severity, summary, labels, now)
    if incident is not None:
        return {"created": True, "incident": incident}
    existing = find_by_fingerprint(fingerprint)
    if existing is None:
        raise sqlite3.IntegrityError(f"fingerprint {fingerprint} vanished")
    return {"created": False, "incident": update_incident(
        existing, alertname, severity, summary, labels, now
    )}


def resolve_incident(fingerprint, now):
    with connect() as db:
        db.execute(
            "UPDATE incidents SET status = ?, resolved_at = ?, last_seen = ?"
            " WHERE fingerprint = ? AND status != ?",
            (STATUS_RESOLVED, now, now, fingerprint, STATUS_RESOLVED),
        )
    return find_by_fingerprint(fingerprint)


def list_incidents():
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM incidents ORDER BY first_seen DESC"
        ).fetchall()
    return [as_dict(row) for row in rows]


def get_incident(incident_id):
    with connect() as db:
        row = db.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
    return as_dict(row)


def mark_notified(incident_id):
    with connect() as db:
        db.execute("UPDATE incidents SET notified = 1 WHERE id = ?", (incident_id,))