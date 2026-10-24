"""Incident index (SQLite) and on-disk evidence bundles.

Two stores on purpose:

* one directory per incident under ``data/incidents/`` holding the evidence as
  plain files, because the coding assistant reads them as files and a human can
  ``cat`` them during an incident;
* a small SQLite index purely for listing and deduplication.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

STATUS_FIRING = "firing"
STATUS_RESOLVED = "resolved"
STATUS_RUNNING = "running"
STATUS_INTERRUPTED = "interrupted"
SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    alertname TEXT NOT NULL,
    severity TEXT NOT NULL,
    endpoint TEXT,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    window_start TEXT,
    window_end TEXT,
    notification_count INTEGER NOT NULL DEFAULT 1,
    bundle_path TEXT,
    agent_status TEXT,
    agent_session_id TEXT,
    agent_exit_code INTEGER,
    agent_summary TEXT,
    agent_error TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved_at TEXT
)
"""

# Added after the first release; SQLite has no IF NOT EXISTS for columns.
MIGRATIONS = {
    "agent_summary": "ALTER TABLE incidents ADD COLUMN agent_summary TEXT",
    "agent_error": "ALTER TABLE incidents ADD COLUMN agent_error TEXT",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: Path, bundle_root: Path):
        self.db_path = Path(db_path)
        self.bundle_root = Path(bundle_root)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.bundle_root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self) -> None:
        with self.connect() as db:
            db.execute(SCHEMA)
            existing = {row["name"] for row in db.execute("PRAGMA table_info(incidents)")}
            for column, statement in MIGRATIONS.items():
                if column not in existing:
                    db.execute(statement)
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS incidents_fingerprint_idx ON incidents (fingerprint)")

    # -- queries ---------------------------------------------------------

    def list_incidents(self) -> list:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM incidents ORDER BY first_seen DESC").fetchall()
        return [dict(row) for row in rows]

    def get_incident(self, incident_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return dict(row) if row else None

    def get_by_fingerprint(self, fingerprint: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM incidents WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return dict(row) if row else None

    # -- mutations -------------------------------------------------------

    def open_incident(self, alert, timestamp: str) -> tuple:
        """Insert or update the incident for this fingerprint.

        Returns ``(created, incident)``. A firing alert reopens a resolved
        incident and bumps the notification counter, mirroring the behaviour of
        the responder already in the repo.
        """
        existing = self.get_by_fingerprint(alert.fingerprint)
        if existing is None:
            incident_id = str(uuid.uuid4())
            try:
                with self.connect() as db:
                    db.execute(
                        """INSERT INTO incidents (
                            id, fingerprint, alertname, severity, endpoint, status, summary,
                            window_start, window_end, notification_count, first_seen, last_seen
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                        (
                            incident_id,
                            alert.fingerprint,
                            alert.alertname,
                            alert.severity,
                            alert.endpoint,
                            STATUS_FIRING,
                            alert.summary,
                            alert.starts_at.isoformat(),
                            alert.ends_at.isoformat() if alert.ends_at else None,
                            timestamp,
                            timestamp,
                        ),
                    )
            except sqlite3.IntegrityError:
                existing = self.get_by_fingerprint(alert.fingerprint)
                if existing is None:
                    raise
                return False, self._update(existing, alert, timestamp)
            return True, self.get_incident(incident_id)

        return False, self._update(existing, alert, timestamp)

    def _update(self, existing: dict, alert, timestamp: str) -> dict:
        with self.connect() as db:
            db.execute(
                """UPDATE incidents SET
                    alertname = ?, severity = ?, endpoint = ?, status = ?, summary = ?,
                    window_start = ?, window_end = ?, notification_count = notification_count + 1,
                    last_seen = ?, resolved_at = NULL
                WHERE id = ?""",
                (
                    alert.alertname,
                    alert.severity,
                    alert.endpoint,
                    STATUS_FIRING,
                    alert.summary,
                    alert.starts_at.isoformat(),
                    alert.ends_at.isoformat() if alert.ends_at else None,
                    timestamp,
                    existing["id"],
                ),
            )
        return self.get_incident(existing["id"])

    def resolve_incident(self, fingerprint: str, timestamp: str):
        with self.connect() as db:
            db.execute(
                """UPDATE incidents SET status = ?, resolved_at = ?, last_seen = ?
                WHERE fingerprint = ? AND status != ?""",
                (STATUS_RESOLVED, timestamp, timestamp, fingerprint, STATUS_RESOLVED),
            )
        return self.get_by_fingerprint(fingerprint)

    def set_bundle_path(self, incident_id: str, path: Path) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE incidents SET bundle_path = ? WHERE id = ?", (str(path), incident_id)
            )

    def set_agent_result(
        self,
        incident_id: str,
        status: str,
        session_id=None,
        exit_code=None,
        summary: str | None = None,
        error: str | None = None,
    ):
        with self.connect() as db:
            db.execute(
                "UPDATE incidents SET agent_status = ?, agent_session_id = ?, agent_exit_code = ?,"
                " agent_summary = COALESCE(?, agent_summary), agent_error = ? WHERE id = ?",
                (status, session_id, exit_code, summary, error, incident_id),
            )

    def mark_running_agents_interrupted(self) -> int:
        """Clear ``running`` rows left behind by a previous process.

        A restart kills the service but not the ``opencode`` child it spawned, so
        without this an incident would claim to be under investigation forever.
        """
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE incidents SET agent_status = ? WHERE agent_status = ?",
                (STATUS_INTERRUPTED, STATUS_RUNNING),
            )
            return cursor.rowcount or 0

    # -- bundle files ----------------------------------------------------

    def bundle_dir(self, incident_id: str, fingerprint: str, created_at: str) -> Path:
        stamp = created_at.replace(":", "").replace("-", "")[:15]
        path = self.bundle_root / f"{stamp}-{fingerprint}-{incident_id[:8]}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_bundle(self, directory: Path, alert, evidence, incident_id: str, bundle_markdown: str) -> dict:
        directory = Path(directory)
        files = {
            "alert": directory / "alert.json",
            "logs": directory / "logs.json",
            "traces": directory / "traces.json",
            "metrics": directory / "metrics.json",
            "bundle": directory / "bundle.md",
        }
        files["alert"].write_text(
            json.dumps(
                {
                    "incident_id": incident_id,
                    "fingerprint": alert.fingerprint,
                    "alertname": alert.alertname,
                    "severity": alert.severity,
                    "status": alert.status,
                    "endpoint": alert.endpoint,
                    "summary": alert.summary,
                    "description": alert.description,
                    "window_start": alert.starts_at.isoformat(),
                    "window_end": alert.ends_at.isoformat() if alert.ends_at else None,
                    "window_seconds": alert.window_seconds,
                    "labels": alert.labels,
                    "annotations": alert.annotations,
                    "values": alert.values,
                    "generator_url": alert.generator_url,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        files["logs"].write_text(
            json.dumps([entry.as_dict() for entry in evidence.logs], indent=2), encoding="utf-8"
        )
        files["traces"].write_text(
            json.dumps([trace.as_dict() for trace in evidence.traces], indent=2), encoding="utf-8"
        )
        files["metrics"].write_text(
            json.dumps({"metrics": evidence.metrics, "errors": evidence.errors}, indent=2),
            encoding="utf-8",
        )
        files["bundle"].write_text(bundle_markdown, encoding="utf-8")
        return {name: path for name, path in files.items()}
