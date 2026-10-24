import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
REPO_DIR = PROJECT_DIR.parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    host: str = field(default_factory=lambda: os.getenv("INCIDENT_RESPONSE_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("INCIDENT_RESPONSE_PORT", 8001))

    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("INCIDENT_DATA_DIR", str(REPO_DIR / "data" / "incidents")))
    )
    index_db: Path = field(
        default_factory=lambda: Path(os.getenv("INCIDENT_INDEX_DB", str(REPO_DIR / "data" / "incidents.db")))
    )

    loki_url: str = field(default_factory=lambda: os.getenv("LOKI_URL", "http://127.0.0.1:3100"))
    tempo_url: str = field(default_factory=lambda: os.getenv("TEMPO_URL", "http://127.0.0.1:3200"))
    prometheus_url: str = field(default_factory=lambda: os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090"))
    grafana_url: str = field(default_factory=lambda: os.getenv("GRAFANA_URL", "http://127.0.0.1:3000"))
    dashboard_uid: str = field(default_factory=lambda: os.getenv("GRAFANA_DASHBOARD_UID", "order-tracker-overview"))

    service_name: str = field(default_factory=lambda: os.getenv("INCIDENT_SERVICE_NAME", "order-tracker"))
    http_timeout: float = field(default_factory=lambda: _float("INCIDENT_HTTP_TIMEOUT", 10.0))

    log_limit: int = field(default_factory=lambda: _int("INCIDENT_LOG_LIMIT", 200))
    trace_limit: int = field(default_factory=lambda: _int("INCIDENT_TRACE_LIMIT", 5))
    span_limit: int = field(default_factory=lambda: _int("INCIDENT_SPAN_LIMIT", 60))
    window_fallback_seconds: int = field(
        default_factory=lambda: _int("INCIDENT_WINDOW_FALLBACK_SECONDS", 900)
    )

    agent_enabled: bool = field(default_factory=lambda: _bool("AGENT_ENABLED", True))
    agent_command: str = field(default_factory=lambda: os.getenv("AGENT_COMMAND", "opencode"))
    agent_model: str = field(default_factory=lambda: os.getenv("AGENT_MODEL", ""))
    agent_dir: Path = field(default_factory=lambda: Path(os.getenv("AGENT_DIR", str(REPO_DIR))))
    agent_timeout: float = field(default_factory=lambda: _float("AGENT_TIMEOUT", 300.0))
    agent_conclusion_timeout: float = field(
        default_factory=lambda: _float("AGENT_CONCLUSION_TIMEOUT", 120.0)
    )
    agent_max_concurrent: int = field(default_factory=lambda: _int("AGENT_MAX_CONCURRENT", 1))

    def bundle_root(self) -> Path:
        return self.data_dir


def load_settings() -> Settings:
    return Settings()
