"""ARGUS Deterministic Fingerprints (Phase 3 §16, §25).

Fingerprints are what stop ARGUS from emitting an anomaly every ten seconds or
an incident per correlated pair. They are pure functions of stable context —
never of a timestamp or a random id — so the same problem in the same place
always produces the same key.

Two layers:

* :func:`anomaly_fingerprint` — one logical anomaly (component + type + metric
  or log template).
* :func:`incident_fingerprint` — one logical incident (scope + primary
  component + dominant anomaly class + time bucket).

The human-readable *material* is returned alongside the hash so explanations can
show exactly what went into the key (§52).
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Iterable, Optional

#: Sentinel used when a scope component is absent, so "all components" is a
#: stable key rather than a ``None`` that stringifies inconsistently.
SCOPELESS = "-"

#: Default incident bucket, in seconds. An incident fingerprint is stable within
#: a bucket and distinct across buckets, so a recurrence hours later is not
#: silently merged into the original incident.
DEFAULT_INCIDENT_BUCKET_SECONDS = 3600


def _normalize(value: object) -> str:
    if value is None:
        return SCOPELESS
    return str(value).strip().lower()


def _stable_hash(material: str) -> str:
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def anomaly_fingerprint_material(
    *,
    project_id: object,
    anomaly_type: object,
    discriminator: Optional[str] = None,
    environment_id: object = None,
    component_id: object = None,
) -> str:
    """Return the human-readable fingerprint material (for explanations).

    Example: ``<project>|production|checkout-service|LATENCY_SPIKE|http.checkout``
    """
    parts = [
        _normalize(project_id),
        _normalize(environment_id),
        _normalize(component_id),
        _normalize(anomaly_type),
        _normalize(discriminator),
    ]
    return "|".join(parts)


def anomaly_fingerprint(
    *,
    project_id: object,
    anomaly_type: object,
    discriminator: Optional[str] = None,
    environment_id: object = None,
    component_id: object = None,
) -> str:
    """Stable 64-hex fingerprint for one logical anomaly.

    ``discriminator`` is the metric name or normalized log template. Two
    different metrics on the same component therefore produce different
    fingerprints, while repeated observations of one metric collapse into one.
    """
    material = anomaly_fingerprint_material(
        project_id=project_id,
        anomaly_type=anomaly_type,
        discriminator=discriminator,
        environment_id=environment_id,
        component_id=component_id,
    )
    return _stable_hash(material)


def bucket_start(
    timestamp: datetime, bucket_seconds: int = DEFAULT_INCIDENT_BUCKET_SECONDS
) -> int:
    """Return the epoch second at the start of ``timestamp``'s bucket.

    Deterministic on UTC instants; a naive datetime is treated as UTC so the
    bucket cannot shift with the host timezone.
    """
    seconds = int(timestamp.timestamp())
    return seconds - (seconds % max(1, bucket_seconds))


def incident_fingerprint_material(
    *,
    project_id: object,
    primary_component_id: object,
    dominant_anomaly_type: object,
    environment_id: object = None,
    time_bucket: object = None,
    related_component_ids: Optional[Iterable[object]] = None,
) -> str:
    """Return the human-readable incident fingerprint material (§25).

    ``related_component_ids`` are sorted before joining so the key does not
    depend on the order anomalies happened to be processed in.
    """
    related = sorted(_normalize(c) for c in (related_component_ids or []))
    parts = [
        _normalize(project_id),
        _normalize(environment_id),
        _normalize(primary_component_id),
        _normalize(dominant_anomaly_type),
        _normalize(time_bucket),
        ",".join(related),
    ]
    return "|".join(parts)


def incident_fingerprint(
    *,
    project_id: object,
    primary_component_id: object,
    dominant_anomaly_type: object,
    environment_id: object = None,
    time_bucket: object = None,
    related_component_ids: Optional[Iterable[object]] = None,
) -> str:
    """Stable 64-hex fingerprint for one logical incident."""
    material = incident_fingerprint_material(
        project_id=project_id,
        primary_component_id=primary_component_id,
        dominant_anomaly_type=dominant_anomaly_type,
        environment_id=environment_id,
        time_bucket=time_bucket,
        related_component_ids=related_component_ids,
    )
    return _stable_hash(material)


__all__ = [
    "SCOPELESS",
    "DEFAULT_INCIDENT_BUCKET_SECONDS",
    "anomaly_fingerprint",
    "anomaly_fingerprint_material",
    "incident_fingerprint",
    "incident_fingerprint_material",
    "bucket_start",
]
