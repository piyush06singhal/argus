# ARGUS Data Model

Phase 0 establishes the ARGUS data model: the entities that represent software systems, their behavior, and their changes. Everything in later phases — knowledge graph, incident intelligence, root-cause analysis — consumes and extends this foundation.

## 1. Conventions

- **UUID primary keys** on every entity (`BaseModel`)
- **Timestamps** everywhere: `created_at` / `updated_at` via `TimestampMixin`, plus domain timestamps (`timestamp`, `start_time`, `deployed_at`, `detected_at`, …)
- **Enum-driven domains** for status, severity, type, and category fields (implemented as PostgreSQL `ENUM`)
- **`metadata`** — a reserved name in SQLAlchemy — is stored via a `metadata_` Pydantic field aliased to `"metadata"` on the wire. Stored as **JSONB** on PostgreSQL, **JSON** on SQLite (tests) via `JSONType`
- **Foreign keys** enforce referential integrity: components belong to a project, incidents belong to a project and optionally an environment, etc.

## 2. Entity relationship overview

```text
projects
 ├── environments
 ├── system_components ──┐
 │   └── component_dependencies (self-referential)
 ├── deployment_events
 ├── observability_events
 ├── incidents
 │    └── incident_evidence
 ├── log_records
 ├── metric_records
 ├── traces
 │    └── spans
 └── code_repositories
```

## 3. Entities

### 3.1 `projects`

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | PK |
| `name` | String(255) | |
| `slug` | String(255) | unique, indexed |
| `description` | Text | optional |
| `status` | Enum `ACTIVE`/`PAUSED`/`ARCHIVED` | default ACTIVE |
| `repository_url` | String(512) | optional |
| `repository_provider` | String(50) | optional |
| `default_branch` | String(100) | optional |
| `metadata` | JSONB | optional |
| `created_at`/`updated_at` | datetime | via mixin |

A project is the container for everything ARGUS analyzes about one software system. Deleting a project cascades to its environments, components, incidents, deployments, and observability data.

### 3.2 `environments`

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | PK |
| `project_id` | UUID → `projects.id` | indexed |
| `name` | String(255) | |
| `environment_type` | Enum `DEVELOPMENT`/`TEST`/`STAGING`/`PRODUCTION`/`CUSTOM` | |
| `description` | Text | optional |
| `is_active` | bool | default true |
| `metadata` | JSONB | optional |

### 3.3 `system_components`

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | PK |
| `project_id` | UUID → `projects.id` | indexed |
| `environment_id` | UUID → `environments.id` | optional, indexed |
| `name` | String(255) | |
| `component_type` | Enum e.g. `SERVICE`, `DATABASE`, `CACHE` | |
| `category` | Enum | e.g. application/infrastructure |
| `status` | Enum | e.g. ACTIVE/DEGRADED/DOWN |
| `description` | Text | optional |
| `owner_team` | String | optional |
| `metadata` | JSONB | |

The `component_dependencies` table models the relationship **graph**. It is self-referential: each row links a `source_component_id` to a `target_component_id`, with a `dependency_type` (e.g. `HTTP`, `DATABASE`, `MESSAGE_QUEUE`) and optional `call_rate`/`timeout_ms` hints. This graph is what every future system-map and blast-radius analysis consumes.

### 3.4 Observability

All observability entities carry `project_id`, optional `environment_id`/`component_id`, and a structured `metadata`/`payload` document. See [docs/observability-model.md](docs/observability-model.md).

| Entity | Table | Key fields |
|--------|-------|------------|
| `ObservabilityEvent` | `observability_events` | `timestamp`, `source`, `event_type`, `severity`, `payload`, `request_id`, `trace_id`, `span_id`, `deployment_id`, `incident_id` |
| `LogRecord` | `log_records` | `timestamp`, `level`, `message`, `service`, `trace_id`, `span_id` |
| `MetricRecord` | `metric_records` | `timestamp`, `metric_name`, `metric_type`, `value`, `unit`, `labels` |
| `TraceRecord` | `traces` | `trace_id` (string), `name`, `start_time`, `end_time`, `duration_ms`, `status` |
| `SpanRecord` | `spans` | `trace_id`, `span_id`, `parent_span_id`, `operation`, `duration_ms`, `status` |

Traces and spans use string IDs (the industry-standard W3C-compatible IDs) rather than UUIDs; `GET /observability/traces/{trace_id}` returns a trace with all its spans, preserving parent/child structure.

### 3.5 Incidents & evidence

| Entity | Table | Key fields |
|--------|-------|------------|
| `Incident` | `incidents` | `title`, `description`, `severity` (`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`), `status` (`OPEN`/`INVESTIGATING`/`RESOLVED`/`CLOSED`), `detected_at`, `started_at`, `resolved_at` |
| `IncidentEvidence` | `incident_evidence` | `evidence_type` (`LOG`/`METRIC`/`TRACE`/`DEPLOYMENT`/`EVENT`), `source_id`, `timestamp`, `relevance_score` (0.0–1.0), `description` |

An incident connects to evidence via `incident_evidence` rows; each row references a specific log/metric/trace/deployment by `source_id`. **Evidence is displayed, never interpreted as root cause in Phase 0.**

### 3.6 Deployments & code repositories

| Entity | Table | Key fields |
|--------|-------|------------|
| `DeploymentEvent` | `deployment_events` | `component_id`, `environment_id`, `version`, `commit_sha`, `deployed_by`, `status` (`SUCCESS`/`FAILED`/`ROLLED_BACK`/…), `deployed_at`, `change_summary` |
| `CodeRepository` | `code_repositories` | `url`, `provider`, `default_branch` |

Deployments are the anchor for future incident ↔ change correlation ("a deployment occurred shortly before the incident").

## 4. Enum domains

| Entity field | Enum values |
|--------------|-------------|
| `ProjectStatus` | ACTIVE, PAUSED, ARCHIVED |
| `EnvironmentType` | DEVELOPMENT, TEST, STAGING, PRODUCTION, CUSTOM |
| `ComponentCategory` / `ComponentStatus` / `DependencyType` | per-domain (see `models/system.py`) |
| `IncidentSeverity` | LOW, MEDIUM, HIGH, CRITICAL |
| `IncidentStatus` | OPEN, INVESTIGATING, RESOLVED, CLOSED |
| `EvidenceType` | LOG, METRIC, TRACE, DEPLOYMENT, EVENT |
| `EventType` / `Severity` | per-domain (see `models/observability.py`) |
| `MetricType` | COUNTER, GAUGE, HISTOGRAM, … |
| `TraceStatus` | OK, ERROR, UNKNOWN, … |
| `DeploymentStatus` | SUCCESS, FAILED, ROLLED_BACK, … |

Pydantic validates these enums at the API boundary, so malformed values are rejected with 422.

## 5. Indexes & performance

- `slug` unique on projects; indexed FKs on `project_id`, `environment_id`, `component_id`
- Timestamp columns are eligible for range queries (`start_time`, `timestamp`, `detected_at`) with pagination on every list endpoint
- No endpoint returns unbounded observability data (`page_size` ≤ 100)

## 6. Migrations

Schema is managed by Alembic (`apps/api/alembic/`). See [docs/development.md](docs/development.md).

## 7. Testing with SQLite

`JSONType` maps to `JSONB` on PostgreSQL and `JSON` on SQLite, so the full model runs against a file-backed SQLite database in tests (fast, isolated). Enum columns behave identically on both backends via SQLAlchemy `Enum` with `native_enum` semantics compatible in both.