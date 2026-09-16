"""ARGUS Models."""
from app.models.base import BaseModel, TimestampMixin
from app.models.project import SoftwareProject, Environment, ProjectStatus, EnvironmentType
from app.models.system import (
    SystemComponent,
    ComponentDependency,
    ComponentCategory,
    ComponentStatus,
    DependencyType,
)
from app.models.observability import (
    ObservabilityEvent,
    LogRecord,
    MetricRecord,
    TraceRecord,
    SpanRecord,
    EventType,
    Severity,
    MetricType,
    TraceStatus,
)
from app.models.incident import (
    Incident,
    IncidentEvidence,
    IncidentSeverity,
    IncidentStatus,
    EvidenceType,
)
from app.models.deployment import (
    DeploymentEvent,
    CodeRepository,
    DeploymentStatus,
)
from app.models.ingestion import (
    ObservabilitySource,
    ObservabilitySourceCategory,
    ObservabilitySourceStatus,
    ConfigurationChangeEvent,
    HealthCheckEvent,
    HealthStatus,
    IngestionFailure,
)

__all__ = [
    # Base
    "BaseModel",
    "TimestampMixin",
    # Projects
    "SoftwareProject",
    "Environment",
    "ProjectStatus",
    "EnvironmentType",
    # System
    "SystemComponent",
    "ComponentDependency",
    "ComponentCategory",
    "ComponentStatus",
    "DependencyType",
    # Observability
    "ObservabilityEvent",
    "LogRecord",
    "MetricRecord",
    "TraceRecord",
    "SpanRecord",
    "EventType",
    "Severity",
    "MetricType",
    "TraceStatus",
    # Incidents
    "Incident",
    "IncidentEvidence",
    "IncidentSeverity",
    "IncidentStatus",
    "EvidenceType",
    # Deployments
    "DeploymentEvent",
    "CodeRepository",
    "DeploymentStatus",
    # Ingestion / sources
    "ObservabilitySource",
    "ObservabilitySourceCategory",
    "ObservabilitySourceStatus",
    "ConfigurationChangeEvent",
    "HealthCheckEvent",
    "HealthStatus",
    "IngestionFailure",
]
