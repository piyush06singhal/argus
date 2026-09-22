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

    # ---- Code intelligence & AI debugger (Phase 6) --------------------------
    #: Master switch. When disabled, existing analyses stay queryable but no new
    #: indexing or debugging run may start.
    CODE_INTELLIGENCE_ENABLED: bool = True
    #: Roots a LOCAL repository path must live under. Empty = any readable path
    #: (single-tenant/dev). Set this in a shared deployment so ARGUS can never be
    #: pointed at ``/etc`` or another tenant's checkout (§56).
    CODE_ALLOWED_ROOTS: List[str] = []
    #: Files larger than this are recorded but never read or parsed (§22 budget).
    CODE_MAX_FILE_BYTES: int = 512_000
    #: Bounded scan: a repository larger than this is indexed as a PARTIAL
    #: snapshot rather than silently truncated.
    CODE_MAX_FILES_PER_SNAPSHOT: int = 20_000
    CODE_MAX_SYMBOLS_PER_FILE: int = 2_000
    #: Lines of a single file a parse will consider.
    CODE_MAX_FILE_LINES: int = 5_000
    #: Directories never descended into.
    CODE_EXCLUDED_DIRS: List[str] = [
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        "dist",
        "build",
        "coverage",
        "htmlcov",
        ".idea",
        ".vscode",
    ]
    #: Wall-clock ceiling for any single git invocation.
    CODE_GIT_TIMEOUT_SECONDS: int = 30
    #: Commits examined when building change history for a file/symbol (§18).
    CODE_HISTORY_MAX_COMMITS: int = 200
    #: Lines of blame gathered per file, and the per-symbol lookback.
    CODE_BLAME_MAX_LINES: int = 2_000
    #: An incremental pass with more changed files than this re-indexes fully
    #: (diffing 5k files costs more than re-parsing them) (§55).
    CODE_INCREMENTAL_MAX_CHANGED_FILES: int = 2_000
    #: Recency window for the RECENTLY_MODIFIED signal (§44).
    CODE_RECENT_CHANGE_DAYS: int = 14
    #: Complexity at/above which HIGH_COMPLEXITY is recorded.
    CODE_COMPLEXITY_SIGNAL_THRESHOLD: int = 10
    #: Fan-in/fan-out at/above which a symbol is flagged (signal, not score).
    CODE_FAN_SIGNAL_THRESHOLD: int = 10

    # ---- AI debugger (Phase 6 §39, §41) ------------------------------------
    #: Model name used by the OpenAI-compatible provider.
    AI_MODEL: str = "gpt-4o-mini"
    #: Base URL for an OpenAI-compatible endpoint (empty = provider default).
    AI_BASE_URL: str = ""
    #: Debugging analysis is deterministic by default (§41).
    AI_TEMPERATURE: float = 0.0
    AI_MAX_TOKENS: int = 4_000
    AI_TIMEOUT_SECONDS: int = 60
    AI_RETRY_COUNT: int = 1
    #: Tool-calling budgets — enforced across a whole session, not per turn (§39).
    DEBUG_MAX_TOOL_CALLS: int = 12
    DEBUG_MAX_TOOL_CALLS_PER_SESSION: int = 120
    DEBUG_MAX_FILES_READ: int = 20
    DEBUG_MAX_LINES_READ: int = 1_500
    DEBUG_MAX_SEARCH_RESULTS: int = 50
    #: Hard ceiling on the assembled context handed to a model (§22).
    DEBUG_MAX_CONTEXT_BYTES: int = 240_000
    DEBUG_MAX_ANALYSIS_SECONDS: int = 120
    #: A question longer than this is rejected rather than truncated silently.
    DEBUG_MAX_QUESTION_CHARS: int = 4_000
    #: Bumped whenever context *selection* rules change (§59).
    DEBUG_CONTEXT_VERSION: str = "1"
    DEBUG_PROMPT_VERSION: str = "1"
    #: Deterministic context assembly is always available; this only decides
    #: whether a model is consulted for hypotheses (§43).
    DEBUG_AI_ENABLED: bool = True
    #: Periodic sweep for code intelligence: closes debug sessions and analysis
    #: runs abandoned by a dead process and un-sticks repositories whose
    #: ``index_status`` is stuck at INDEXING (§55–§57).
    CODE_SWEEP_ENABLED: bool = True
    CODE_SWEEP_INTERVAL_SECONDS: int = 120
    #: How many rows per table one sweep pass may touch, so a pathological
    #: backlog cannot become one enormous transaction.
    CODE_SWEEP_BATCH: int = 200
    #: Retention for Phase 6 indexed snapshots and debug sessions.
    RETENTION_CODE_SNAPSHOTS: int = 365
    #: Phase 7 — patch verification workspace hygiene. Workspaces left by a
    #: dead process are destroyed by the sweep after this grace period (§48).
    FIX_SWEEP_ENABLED: bool = True
    FIX_SWEEP_INTERVAL_SECONDS: int = 120
    FIX_WORKSPACE_GRACE_SECONDS: int = 3600
    #: Phase 7 — per-command budget ceilings the registry may not exceed.
    FIX_COMMAND_TIMEOUT_SECONDS: int = 900

    # --- Phase 8: predictive reliability (§3, §5, §18, §19–§21, §39, §59) ---
    #: Master switch for forecast generation.
    RELIABILITY_FORECASTING_ENABLED: bool = True
    #: The primary observation window features are computed over.
    RELIABILITY_FEATURE_WINDOW_SECONDS: int = 3600
    #: A longer reference window used only for "versus baseline" deviations.
    RELIABILITY_BASELINE_WINDOW_SECONDS: int = 86400
    #: Below this many usable samples a series cannot carry a forecast.
    RELIABILITY_MIN_SAMPLES: int = 5
    #: Telemetry older than this before ``forecast_time`` marks data as stale.
    RELIABILITY_STALE_TELEMETRY_SECONDS: int = 3600
    #: A minimum share by which the observed pace must exceed the expected
    #: pace before "failure frequency is increasing" may be signalled.
    RELIABILITY_FREQUENCY_TREND_RATIO: float = 0.25
    #: Horizons generated by default, by enum name (§3).
    RELIABILITY_DEFAULT_HORIZONS: List[str] = [
        "ONE_HOUR",
        "SIX_HOURS",
        "TWENTY_FOUR_HOURS",
        "SEVEN_DAYS",
    ]
    #: Every prediction type generated by default, by enum name (§4).
    RELIABILITY_DEFAULT_PREDICTION_TYPES: List[str] = [
        "FAILURE_RISK",
        "ERROR_RATE_RISK",
        "LATENCY_RISK",
        "AVAILABILITY_RISK",
        "RESOURCE_EXHAUSTION_RISK",
        "DEPENDENCY_FAILURE_RISK",
        "REGRESSION_RISK",
        "INCIDENT_RISK",
        "RELIABILITY_DEGRADATION",
    ]
    #: Bounded work per sweep so one sweep can never scan a whole tenant (§59).
    RELIABILITY_MAX_COMPONENTS_PER_RUN: int = 200
    RELIABILITY_MAX_METRIC_SERIES: int = 40
    RELIABILITY_MAX_SAMPLES_PER_METRIC: int = 2000
    RELIABILITY_MAX_SIGNALS_PER_FORECAST: int = 8
    RELIABILITY_MAX_DEPENDENCY_HOPS: int = 3

    #: Centralized risk policy (§5). Risk score bands, ascending.
    RELIABILITY_THRESHOLD_MEDIUM: float = 0.35
    RELIABILITY_THRESHOLD_HIGH: float = 0.60
    RELIABILITY_THRESHOLD_CRITICAL: float = 0.80
    #: Data-coverage bands (§18).
    RELIABILITY_MIN_COVERAGE: float = 0.66
    RELIABILITY_GOOD_COVERAGE: float = 1.0

    #: Deterministic predictor parameters (§20–§22).
    RELIABILITY_EWMA_ALPHA: float = 0.4
    RELIABILITY_FLAT_EPSILON: float = 0.02
    RELIABILITY_VOLATILE_RATIO: float = 0.35
    RELIABILITY_STRONG_SLOPE: float = 0.15
    #: Hard bound on linear projection growth (§20).
    RELIABILITY_MAX_PROJECTION_GROWTH: float = 3.0
    #: Share of a configured ceiling at which a resource counts as saturated.
    RELIABILITY_SATURATION_RATIO: float = 0.85
    #: Ceilings. ``None`` disables saturation detection for that metric.
    RELIABILITY_LATENCY_CEILING_MS: Optional[float] = 2000.0
    RELIABILITY_ERROR_RATE_CEILING: Optional[float] = 0.1
    RELIABILITY_CPU_CEILING_PERCENT: Optional[float] = 100.0
    RELIABILITY_MEMORY_CEILING_PERCENT: Optional[float] = 100.0
    RELIABILITY_DISK_CEILING_PERCENT: Optional[float] = 100.0
    RELIABILITY_QUEUE_CEILING: Optional[float] = 1000.0
    RELIABILITY_CONNECTION_CEILING: Optional[float] = 100.0

    #: Lifecycle / early warning (§27, §39, §40).
    RELIABILITY_WARNING_COOLDOWN_SECONDS: int = 1800
    RELIABILITY_WARNING_MIN_RISK_LEVEL: str = "HIGH"
    #: Hours of history consulted for historical incident similarity (§36).
    RELIABILITY_SIMILARITY_LOOKBACK_DAYS: int = 90

    #: Evaluation (§28–§29). Below this many scored forecasts no metric is
    #: reported — "insufficient sample size" instead of a misleading number.
    RELIABILITY_MIN_EVALUATION_SAMPLE: int = 30
    RELIABILITY_EVALUATION_GRACE_SECONDS: int = 60
    #: A forecast at or above this level counts as a positive prediction.
    RELIABILITY_POSITIVE_RISK_LEVELS: List[str] = ["HIGH", "CRITICAL"]

    #: Backtesting (§30, §31).
    RELIABILITY_BACKTEST_MAX_STEPS: int = 200
    RELIABILITY_BACKTEST_MIN_TRAINING_SECONDS: int = 3600

    #: ML data-sufficiency gate (§24). A model that fails any check falls back
    #: to the deterministic baseline; it is never trained on fabricated data.
    RELIABILITY_ML_MIN_SAMPLES: int = 200
    RELIABILITY_ML_MIN_POSITIVES: int = 30
    RELIABILITY_ML_MIN_NEGATIVES: int = 30
    RELIABILITY_ML_MIN_COVERAGE: float = 0.8

    #: Drift monitoring (§41, §42). Watch and flag bands, plus the windows the
    #: reference and current distributions are drawn from.
    RELIABILITY_DRIFT_WATCH_THRESHOLD: float = 0.2
    RELIABILITY_DRIFT_FLAG_THRESHOLD: float = 0.4
    RELIABILITY_DRIFT_REFERENCE_WINDOW_SECONDS: int = 604800
    RELIABILITY_DRIFT_CURRENT_WINDOW_SECONDS: int = 86400

    #: Scheduling (§57, §58).
    RELIABILITY_ASYNC: bool = True
    RELIABILITY_SWEEP_ENABLED: bool = True
    RELIABILITY_SWEEP_INTERVAL_SECONDS: int = 300
    RELIABILITY_SWEEP_BATCH: int = 50

    #: Retention: forecasts and their derived rows are pruned after N days.
    RETENTION_RELIABILITY_FORECASTS: int = 180
    RETENTION_RELIABILITY_EVALUATIONS: int = 365

    #: Optional narrative layer on the forecast explanation (§34). "none" keeps
    #: the API fully deterministic; "deterministic" composes prose from stored
    #: rows without a model; "model" re-words the explanation with the configured
    #: AI provider. A mock provider is treated as none, because fabricated prose
    #: about a real system is worse than no prose.
    RELIABILITY_NARRATIVE_PROVIDER: str = "none"

    # ------------------------------------------------------------------
    # Phase 9 — Safe Autonomous Remediation (§40, §41, §45)
    # ------------------------------------------------------------------
    #: Master kill switch. When false, **no** action may apply a live effect
    #: anywhere, whatever a project's policy says. This is the switch an
    #: operator flips in an incident, so it is deliberately independent of the
    #: per-scope policy engine: losing the database of policies must not be
    #: able to turn execution back on.
    REMEDIATION_EXECUTION_ENABLED: bool = True
    #: The regime used when a project has no policy row. ``OBSERVE_ONLY`` — a
    #: missing policy is a restrictive default, never a permissive one (§2).
    REMEDIATION_DEFAULT_MODE: str = "OBSERVE_ONLY"
    #: Environments whose names mark them as non-production. Autonomous
    #: execution is only ever considered here, and only for low-risk actions.
    REMEDIATION_NON_PRODUCTION_ENVIRONMENT_NAMES: List[str] = [
        "development",
        "dev",
        "test",
        "testing",
        "staging",
        "sandbox",
        "local",
        "preview",
        "demo",
    ]
    #: Action types execution is willing to attempt at all. Anything absent is
    #: refused with ``ENVIRONMENT_NOT_ALLOWED`` before a handler is selected.
    REMEDIATION_ENABLED_ACTION_TYPES: List[str] = [
        "PAUSE_BACKGROUND_JOB",
        "RESUME_BACKGROUND_JOB",
        "DISABLE_FEATURE_FLAG",
        "ENABLE_FEATURE_FLAG",
        "DISABLE_DEGRADED_DEPENDENCY",
    ]
    #: ARGUS-owned feature flags a plan may target (§10). An unknown flag name
    #: is a validation failure, not a no-op.
    REMEDIATION_KNOWN_FEATURE_FLAGS: List[str] = [
        "anomaly_detection",
        "reliability_forecasting",
        "code_indexing",
        "fix_verification",
        "reproduction_execution",
        "graph_extraction",
    ]
    #: Background jobs a plan may pause (§10).
    REMEDIATION_KNOWN_BACKGROUND_JOBS: List[str] = [
        "ingestion_worker",
        "anomaly_sweep",
        "reliability_sweep",
        "code_sweep",
        "fix_sweep",
        "reproduction_sweep",
        "remediation_sweep",
    ]

    #: Hard ceilings the policy engine cannot exceed, whatever a policy says.
    #: Configuration is a *narrowing* mechanism: no row in the database can
    #: raise these (§21, §45).
    REMEDIATION_HARD_MAX_RISK_AUTONOMOUS: str = "LOW"
    REMEDIATION_HARD_MAX_ACTIONS_PER_WINDOW: int = 20
    REMEDIATION_HARD_MAX_CONCURRENT_ACTIONS: int = 3
    REMEDIATION_HARD_MAX_BLAST_RADIUS_PERCENT: float = 50.0
    REMEDIATION_HARD_EXECUTION_TIMEOUT_SECONDS: int = 600
    #: Widest blast-radius scope any action may reach, as a structural ceiling.
    #: The default is the widest scope ARGUS defines, because the *per-action*
    #: bound is already the action definition's own ``maximum_blast_radius`` and a
    #: process ceiling narrower than a registered action's scope would make that
    #: action permanently unauthorizable — every control-plane action ARGUS can
    #: actually execute is environment-scoped, so a narrower default would leave
    #: the phase able to propose and unable to do anything. Narrowing this to
    #: ``SINGLE_COMPONENT`` (or lower) is a deliberate operator choice to forbid
    #: wider reaches; it is applied to every stored policy on top of its own
    #: limits and can only ever make the effective policy stricter.
    REMEDIATION_HARD_MAX_BLAST_RADIUS_SCOPE: str = "ENVIRONMENT"

    #: Guard-rail defaults for the fallback policy (§25, §27).
    REMEDIATION_ACTION_WINDOW_SECONDS: int = 3600
    REMEDIATION_DEFAULT_MAX_ACTIONS_PER_WINDOW: int = 5
    REMEDIATION_DEFAULT_COOLDOWN_SECONDS: int = 60
    REMEDIATION_DEFAULT_MAX_CONCURRENT_ACTIONS: int = 1
    REMEDIATION_CIRCUIT_FAILURE_THRESHOLD: int = 3
    REMEDIATION_CIRCUIT_RESET_SECONDS: int = 900
    REMEDIATION_CIRCUIT_HALF_OPEN_PROBES: int = 1
    REMEDIATION_MAX_EXECUTION_ATTEMPTS: int = 2
    REMEDIATION_EXECUTION_TIMEOUT_SECONDS: int = 120
    REMEDIATION_APPROVAL_TTL_SECONDS: int = 1800
    REMEDIATION_ACTION_EXPIRY_SECONDS: int = 86400

    #: Canary (§23).
    REMEDIATION_CANARY_ENABLED: bool = True
    REMEDIATION_CANARY_PERCENT: float = 10.0

    #: Verification window (§28–§31). An action is verified over a window of
    #: real telemetry, not by reading back its own command exit code.
    REMEDIATION_VERIFICATION_WINDOW_SECONDS: int = 300
    REMEDIATION_VERIFICATION_GRACE_SECONDS: int = 30
    REMEDIATION_MAX_VERIFICATION_ATTEMPTS: int = 2
    #: A verification needs at least this many samples to be conclusive; below
    #: it the verdict is INCONCLUSIVE rather than a pass (§30).
    REMEDIATION_VERIFICATION_MIN_SAMPLES: int = 3
    #: Tolerance for "not worse than baseline" comparisons (relative).
    REMEDIATION_ERROR_RATE_TOLERANCE: float = 0.2
    REMEDIATION_LATENCY_TOLERANCE: float = 0.25

    #: Planning (§8, §14).
    REMEDIATION_PLANNER_ENABLED: bool = True
    REMEDIATION_MAX_PROPOSALS_PER_RUN: int = 10
    REMEDIATION_PROPOSAL_DEDUP_WINDOW_SECONDS: int = 3600
    #: Confidence floor below which the planner stores a proposal as
    #: NON-actionable rather than proposing execution (§9).
    REMEDIATION_MIN_PROPOSAL_CONFIDENCE: float = 0.2

    #: Scheduling (§39).
    REMEDIATION_ASYNC: bool = True
    REMEDIATION_SWEEP_ENABLED: bool = True
    REMEDIATION_SWEEP_INTERVAL_SECONDS: int = 60
    REMEDIATION_SWEEP_BATCH: int = 25
    #: Retention: remediation history is the audit trail; it is pruned only
    #: when explicitly configured to a positive number of days.
    RETENTION_REMEDIATION_ACTIONS: int = 730

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
