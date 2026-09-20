"""ARGUS Core Configuration."""

from __future__ import annotations

from typing import Any, List, Optional
from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    APP_NAME: str = "ARGUS"
    APP_VERSION: str = "0.1.0"
    APP_DESCRIPTION: str = "Autonomous Software Reliability & Engineering Intelligence"
    API_ENVIRONMENT: str = "development"
    API_DEBUG: bool = False

    # Database
    DATABASE_URL: str = (
        "postgresql+asyncpg://argus:argus_password@localhost:5432/argus_db"
    )
    DATABASE_HOST: str = "localhost"
    DATABASE_PORT: int = 5432
    DATABASE_NAME: str = "argus_db"
    DATABASE_USER: str = "argus"
    DATABASE_PASSWORD: str = "argus_password"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379

    # API
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_WORKERS: int = 4
    CORS_ORIGINS: List[str] = ["http://localhost:3000"]

    # Authentication (future use)
    AUTH_SECRET: str = "change-this-in-production"
    AUTH_ALGORITHM: str = "HS256"
    AUTH_TOKEN_EXPIRY: int = 3600

    # AI Provider (future use)
    AI_PROVIDER: str = "mock"
    AI_API_KEY: Optional[str] = None

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"

    # Ingestion
    INGESTION_BATCH_SIZE: int = 100
    INGESTION_FLUSH_INTERVAL: int = 5
    INGESTION_WORKER_ENABLED: bool = True
    # Payload limits (§27) — protected against oversized observability payloads.
    MAX_LOG_MESSAGE_LENGTH: int = 8192
    MAX_METADATA_LENGTH: int = 10000
    MAX_METRIC_LABELS: int = 32
    MAX_LABEL_KEY_LENGTH: int = 128
    MAX_LABEL_VALUE_LENGTH: int = 512

    # Data Retention (days)
    RETENTION_LOGS: int = 90
    RETENTION_METRICS: int = 90
    RETENTION_TRACES: int = 30
    RETENTION_EVENTS: int = 90

    # Knowledge graph (Phase 2)
    #: Edges whose last evidence is older than this many days turn STALE.
    GRAPH_STALE_AFTER_DAYS: int = 30
    #: Enqueue a ``graph_extract`` job after trace ingestion (async pipeline).
    GRAPH_EXTRACT_ASYNC: bool = True
    #: Upper bound on spans/events loaded per graph-extract job (§59).
    GRAPH_EXTRACT_SPAN_LIMIT: int = 5000

    # Anomaly & incident intelligence (Phase 3)
    #: Master switch for detection (tests / synchronous deployments).
    ANOMALY_DETECTION_ENABLED: bool = True
    #: Enqueue an ``anomaly_detect`` job after telemetry ingestion.
    ANOMALY_DETECTION_ASYNC: bool = True
    #: Periodic rolling-window sweep so sparse/delayed telemetry is evaluated.
    ANOMALY_SWEEP_ENABLED: bool = True
    ANOMALY_SWEEP_INTERVAL_SECONDS: int = 60
    #: Defaults applied to rules that omit a value (§9, §18).
    ANOMALY_DEFAULT_WINDOW_SECONDS: int = 300
    ANOMALY_DEFAULT_COOLDOWN_SECONDS: int = 300
    ANOMALY_DEFAULT_MIN_SAMPLES: int = 5
    #: Default z-score cutoff for Z_SCORE conditions.
    ANOMALY_Z_THRESHOLD: float = 3.0
    #: Bounded work per detection run (§47) — never an unbounded scan.
    ANOMALY_MAX_ANOMALIES_PER_RUN: int = 500
    ANOMALY_MAX_TELEMETRY_SAMPLES: int = 5000
    ANOMALY_OBSERVATION_LIMIT: int = 200
    #: Correlation window — anomalies spread wider than this never merge (§22).
    CORRELATION_WINDOW_SECONDS: int = 300
    CORRELATION_MAX_ANOMALIES: int = 200
    #: Max graph hops used for structural (not causal) context (§23).
    CORRELATION_MAX_COMPONENT_HOPS: int = 2
    #: How far around the first anomaly to look for deployments/config changes.
    INCIDENT_CONTEXT_WINDOW_SECONDS: int = 900
    INCIDENT_MAX_GRAPH_NODES: int = 200
    #: Retention for Phase 3 rows (swept like other telemetry).
    RETENTION_ANOMALIES: int = 90
    RETENTION_INCIDENTS: int = 365

    # ---- Causal analysis (Phase 4) -----------------------------------------
    #: Master switch — analysis endpoints/report still work when disabled;
    #: running a *new* analysis is refused (results stay queryable).
    CAUSAL_ANALYSIS_ENABLED: bool = True
    #: How far back from incident onset to gather evidence (§46 bounded windows).
    CAUSAL_EVIDENCE_WINDOW_SECONDS: int = 3600
    #: How far past the last anomaly to look for recovery evidence (§31).
    CAUSAL_RECOVERY_WINDOW_SECONDS: int = 1800
    #: Candidate budget — a hypothesis nobody can read is not a hypothesis (§22).
    CAUSAL_MAX_CANDIDATES: int = 8
    #: Per-analysis evidence cap across all categories.
    CAUSAL_MAX_EVIDENCE: int = 200
    #: Traces examined for directional evidence per analysis (§15, §46).
    CAUSAL_MAX_TRACES: int = 100
    #: Max dependency-graph hops when building the structural context (§13).
    CAUSAL_MAX_DEPENDENCY_HOPS: int = 4
    #: A change within this window of onset is *temporally relevant* (§17).
    CAUSAL_CHANGE_PROXIMITY_SECONDS: int = 900
    #: Minimum score share (0..1) the top candidate needs for primary selection;
    #: otherwise primary stays UNKNOWN (§29 — never force an answer).
    CAUSAL_PRIMARY_MIN_SCORE: float = 0.5
    #: Gap (seconds) between chain links beyond which the link is a correlation,
    #: not a propagation (§33 chain validation).
    CAUSAL_CHAIN_MAX_GAP_SECONDS: int = 600
    #: Retention for Phase 4 analysis rows (swept like other telemetry).
    RETENTION_CAUSAL_ANALYSES: int = 365

    # ---- Failure reproduction (Phase 5) ------------------------------------
    #: Master switch. When disabled, experiments stay queryable but no new
    #: experiment may be planned, started, or executed (§44).
    REPRODUCTION_ENABLED: bool = True
    #: Which isolation mechanism provisions sandboxes.
    #: ``local``  — process-isolated subprocesses on loopback (always available).
    #: ``docker`` — unprivileged containers on an internal network (opt-in).
    REPRO_SANDBOX_BACKEND: str = "local"
    #: Root directory for sandbox working trees. Empty = a directory under the
    #: system temp dir. Sandboxes are created *inside* this root and are the
    #: only thing ARGUS ever writes to during an experiment.
    REPRO_SANDBOX_ROOT: str = ""
    #: Where experiment artifacts are stored. Artifacts outlive the sandbox that
    #: produced them (they are the audit trail, §41), so they live outside the
    #: disposable working tree. Empty = ``<cwd>/var/reproduction``.
    REPRO_ARTIFACT_ROOT: str = ""
    #: Container image used by the Docker sandbox backend. It only needs a
    #: Python 3 interpreter; the runner is mounted read-only.
    REPRO_DOCKER_IMAGE: str = "python:3.11-alpine"
    #: Wall-clock ceiling for a single experiment run, enforced by the
    #: orchestrator and by a reaper for crashed workers (§39).
    REPRO_EXPERIMENT_TIMEOUT_SECONDS: int = 300
    #: Ceiling for provisioning (sandbox create + service readiness).
    REPRO_PROVISION_TIMEOUT_SECONDS: int = 60
    #: Conservative per-sandbox resource limits (§40).
    REPRO_MAX_CPU_SECONDS: int = 60
    REPRO_MAX_MEMORY_MB: int = 512
    REPRO_MAX_DISK_MB: int = 64
    REPRO_MAX_PROCESSES: int = 32
    REPRO_MAX_REPLAY_REQUESTS: int = 200
    REPRO_MAX_TELEMETRY_SIGNALS: int = 5000
    #: Cap on captured telemetry bytes per sandbox (log size limit §40).
    REPRO_MAX_TELEMETRY_BYTES: int = 8_000_000
    #: Default replay mode; concurrency is opt-in (§19).
    REPRO_DEFAULT_REPLAY_MODE: str = "SEQUENTIAL"
    #: Maximum reproducible repetitions for determinism detection (§34, §35).
    REPRO_MAX_REPETITIONS: int = 10
    REPRO_DEFAULT_REPETITIONS: int = 1
    #: Network policy for sandboxes. ``ISOLATED`` is the default (§13).
    REPRO_NETWORK_POLICY: str = "ISOLATED"
    #: Hosts a sandbox may reach when the policy is ``CONTROLLED_EGRESS``.
    REPRO_EGRESS_ALLOWLIST: List[str] = []
    #: Forward captured reproduction telemetry through the Phase 1 ingestion
    #: pipeline into a dedicated reproduction environment (§23). Off by default:
    #: an experiment must never be able to pollute production telemetry (§24).
    REPRO_FORWARD_TELEMETRY: bool = False
    #: Compare reproduced behaviour against the original incident with a
    #: tolerance window (seconds) when aligning event sequences (§27).
    REPRO_SEQUENCE_TOLERANCE_SECONDS: int = 120
    #: How many replay inputs a single plan may select from the incident.
    REPRO_MAX_INPUTS: int = 50
    #: NOTE: there is deliberately no "keep the sandbox" knob. §55 makes cleanup
    #: unconditional — every experiment destroys its sandbox, and any that could
    #: not be destroyed is reported through the reproduction metrics rather than
    #: remembered on disk. A retention flag here would be a switch that promises
    #: to preserve a sandbox and cannot honour it.
    #: Periodic reaper for reproduction experiments: closes experiments whose
    #: deadline passed with no worker driving them, and destroys sandboxes whose
    #: experiment is finished (§39, §55). Without it, a killed worker leaks a
    #: sandbox and leaves the experiment stuck in a non-terminal state.
    REPRO_SWEEP_ENABLED: bool = True
    REPRO_SWEEP_INTERVAL_SECONDS: int = 60
    #: Retention for Phase 5 rows (swept like other telemetry).
    RETENTION_REPRODUCTIONS: int = 180

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            import json

            return json.loads(v)
        return v

    def model_post_init(self, __context: Any) -> None:
        """Derive REDIS_HOST/PORT from REDIS_URL unless explicitly configured.

        This keeps the Redis health check correct wherever the app runs
        (local, Docker Compose, cluster) as long as REDIS_URL is set.
        """
        if (
            "REDIS_HOST" not in self.model_fields_set
            or "REDIS_PORT" not in self.model_fields_set
        ):
            parsed = urlparse(self.REDIS_URL)
            if parsed.hostname:
                self.REDIS_HOST = parsed.hostname
            if parsed.port:
                self.REDIS_PORT = parsed.port

    @property
    def is_development(self) -> bool:
        return self.API_ENVIRONMENT == "development"

    @property
    def is_testing(self) -> bool:
        return self.API_ENVIRONMENT == "test"

    @property
    def is_production(self) -> bool:
        return self.API_ENVIRONMENT == "production"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


def get_settings() -> Settings:
    """Get application settings."""
    return Settings()
