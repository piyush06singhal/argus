"""ARGUS Environment Snapshot Service (Phase 5 §14, §15, §33).

Captures what an environment *is* — versions, configuration, topology, feature
flags, resource limits — so a reproduction can be compared against the system it
claims to reproduce, and so differences are reported rather than assumed away.

Two properties make this trustworthy:

* **Sanitized before it is stored.** Configuration and environment variables are
  run through :class:`ConfigSanitizer` first; only the sanitization *report* (what
  kind of thing was replaced, and how many) is kept alongside, never the value.
* **Differences carry severity.** A different dependency version or a different
  service topology is *major* — it can change the failure itself. A different
  feature flag or resource limit is *minor* — it can change how easily the
  failure appears. The validator (§31) turns any major difference into a cap on
  confidence, so a mismatched sandbox cannot produce a confident verdict.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from app.core.config import get_settings
from app.services.reproduction_sandbox import (
    SandboxHandle,
    load_environment_descriptor,
)
from app.services.reproduction_sanitizer import ConfigSanitizer, SanitizationReport

settings = get_settings()

MAJOR = "major"
MINOR = "minor"
EQUAL = "equal"


@dataclass
class SnapshotData:
    """A sanitized environment description, ready to store and diff."""

    source: str
    captured_at: datetime
    label: Optional[str] = None
    application_version: Optional[str] = None
    schema_version: Optional[str] = None
    runtime_versions: dict[str, Any] = field(default_factory=dict)
    dependency_versions: dict[str, Any] = field(default_factory=dict)
    configuration: dict[str, Any] = field(default_factory=dict)
    environment_variables: dict[str, Any] = field(default_factory=dict)
    feature_flags: dict[str, Any] = field(default_factory=dict)
    service_topology: dict[str, Any] = field(default_factory=dict)
    resource_limits: dict[str, Any] = field(default_factory=dict)
    sanitization: dict[str, Any] = field(default_factory=dict)
    content_hash: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


#: Fields whose mismatch can change the failure itself.
_MAJOR_FIELDS = (
    "application_version",
    "schema_version",
    "dependency_versions",
    "service_topology",
)
#: Fields whose mismatch changes how *easily* the failure appears.
_MINOR_FIELDS = (
    "configuration",
    "feature_flags",
    "resource_limits",
    "runtime_versions",
)


class EnvironmentSnapshotService:
    """Captures, sanitizes, and diffs environment descriptions."""

    def __init__(self, sanitizer: Optional[ConfigSanitizer] = None) -> None:
        self._sanitizer = sanitizer or ConfigSanitizer()

    # -- capture ---------------------------------------------------------
    def capture_original(
        self,
        *,
        template: str,
        environment_name: Optional[str] = None,
        overrides: Optional[dict[str, Any]] = None,
    ) -> SnapshotData:
        """Capture the declared shape of the system being reproduced.

        The descriptor is the demo system's own declared environment; it is
        stored sanitized, and the sanitization report travels with it so a diff
        can state that both sides were captured under the same treatment.
        """
        descriptor = load_environment_descriptor(template)
        merged = dict(descriptor or {})
        if overrides:
            merged.update(overrides)
        config, config_report = self._sanitizer.sanitize_config(
            merged.get("configuration") or {}
        )
        env_vars, env_report = self._sanitizer.sanitize_env(
            {k: str(v) for k, v in (merged.get("environment_variables") or {}).items()}
        )
        snapshot = SnapshotData(
            source="ORIGINAL",
            captured_at=datetime.now(timezone.utc),
            label=environment_name or merged.get("label") or template,
            application_version=merged.get("application_version"),
            schema_version=merged.get("schema_version"),
            runtime_versions=dict(merged.get("runtime_versions") or {}),
            dependency_versions=dict(merged.get("dependency_versions") or {}),
            configuration=config,
            environment_variables=env_vars,
            feature_flags=dict(merged.get("feature_flags") or {}),
            service_topology=dict(merged.get("service_topology") or {}),
            resource_limits=dict(merged.get("resource_limits") or {}),
            sanitization=_merge_reports(config_report, env_report),
            metadata={
                "descriptor": template,
                "declared_not_captured": True,
                "note": (
                    "This snapshot describes the declared shape of the reproduced "
                    "system. It is not a capture of the production runtime."
                ),
            },
        )
        snapshot.content_hash = self.content_hash(snapshot)
        return snapshot

    def capture_sandbox(self, handle: SandboxHandle, *, template: str) -> SnapshotData:
        """Capture what the sandbox actually is.

        Read from the sandbox's own metadata (the resolved service configs, the
        effective limits, the runner version) rather than from the template, so
        the snapshot describes the environment that ran.
        """
        configs = handle.metadata.get("service_configs", {})
        limits = handle.metadata.get("limits", {})
        container_ports: dict[str, Any] = handle.metadata.get("container_ports") or {}
        # Expressed in the *declared* vocabulary (``depends_on``/``role``), so a
        # topology difference means a different topology and not a different way
        # of writing the same one down. A false MAJOR difference here would cap
        # confidence in every verdict the experiment produces (§33).
        topology = {
            name: {
                "depends_on": (
                    [config["dependency"]] if config.get("dependency") else []
                ),
                "role": config.get("role"),
            }
            for name, config in configs.items()
        }
        descriptor = load_environment_descriptor(template) or {}
        dependency_versions = {
            name: str(config.get("app_version") or "unknown")
            for name, config in configs.items()
        }
        configuration = {
            name: {
                key: config.get(key)
                for key in ("timeout_ms", "base_latency_ms", "pool_size", "error_rate")
                if config.get(key) is not None
            }
            for name, config in configs.items()
        }
        service_env, env_report = self._sanitizer.sanitize_env(
            {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONUNBUFFERED": "1",
                "ARGUS_SANDBOX": handle.sandbox_key,
            }
        )
        snapshot = SnapshotData(
            source="SANDBOX",
            captured_at=datetime.now(timezone.utc),
            label=handle.sandbox_key,
            # The entry service is the system's version: the first service in a
            # dependency-ordered map is the datastore, whose version says nothing
            # about the application being reproduced.
            application_version=_entry_app_version(
                configs, str(handle.metadata.get("entry_service") or "")
            ),
            # The *reproduced system's* schema, not ARGUS's own migration head:
            # conflating the two reported a schema mismatch on every experiment.
            # A template that declares no schema is reported as unknown rather
            # than given ARGUS's version.
            schema_version=(
                str(descriptor["schema_version"])
                if descriptor.get("schema_version")
                else None
            ),
            runtime_versions={
                "python": platform.python_version(),
                "python_implementation": sys.implementation.name,
                "platform": platform.system().lower(),
            },
            dependency_versions=dependency_versions,
            configuration=configuration,
            environment_variables=service_env,
            feature_flags={
                "checkout_retries": False,
                "inventory_cache": False,
                "db_connection_pooling": True,
                "circuit_breaker": False,
            },
            service_topology=topology,
            resource_limits=dict(limits),
            sanitization=_merge_reports(SanitizationReport(), env_report),
            metadata={
                "backend": handle.backend.value,
                "network_policy": handle.network_policy.value,
                "sandbox_key": handle.sandbox_key,
                "container_ports": container_ports,
                "workdir": str(handle.root_path),
            },
        )
        snapshot.content_hash = self.content_hash(snapshot)
        return snapshot

    # -- utilities -------------------------------------------------------
    @staticmethod
    def content_hash(snapshot: SnapshotData) -> str:
        """Hash the comparable fields, so two identical captures hash alike."""
        payload = {
            "application_version": snapshot.application_version,
            "schema_version": snapshot.schema_version,
            "runtime_versions": snapshot.runtime_versions,
            "dependency_versions": snapshot.dependency_versions,
            "configuration": snapshot.configuration,
            "feature_flags": snapshot.feature_flags,
            "service_topology": snapshot.service_topology,
            "resource_limits": snapshot.resource_limits,
            "environment_variables": sorted(snapshot.environment_variables),
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def diff(
        self, original: Optional[SnapshotData], sandbox: Optional[SnapshotData]
    ) -> list[dict[str, Any]]:
        """Compare two snapshots, labelling every difference with a severity."""
        if original is None or sandbox is None:
            return [
                {
                    "field": "snapshot",
                    "severity": MINOR,
                    "original": original is not None,
                    "sandbox": sandbox is not None,
                    "note": "One side of the comparison is missing.",
                }
            ]
        differences: list[dict[str, Any]] = []
        for field_name in _MAJOR_FIELDS:
            value_original = getattr(original, field_name, None)
            value_sandbox = getattr(sandbox, field_name, None)
            if _normalize(value_original) == _normalize(value_sandbox):
                continue
            differences.append(
                {
                    "field": field_name,
                    "severity": MAJOR,
                    "original": value_original,
                    "sandbox": value_sandbox,
                    "changed_keys": _changed_keys(value_original, value_sandbox),
                    "note": (
                        "A difference here can change the failure itself, so it "
                        "caps confidence in any verdict."
                    ),
                }
            )
        for field_name in _MINOR_FIELDS:
            value_original = getattr(original, field_name, None)
            value_sandbox = getattr(sandbox, field_name, None)
            if _normalize(value_original) == _normalize(value_sandbox):
                continue
            differences.append(
                {
                    "field": field_name,
                    "severity": MINOR,
                    "original": value_original,
                    "sandbox": value_sandbox,
                    "changed_keys": _changed_keys(value_original, value_sandbox),
                    "note": (
                        "A difference here changes how easily the failure appears, "
                        "not whether it is possible."
                    ),
                }
            )
        return differences

    def has_major_differences(self, differences: Sequence[dict[str, Any]]) -> bool:
        return any(str(item.get("severity")) == MAJOR for item in differences)


def _entry_app_version(configs: dict[str, Any], entry_service: str) -> Optional[str]:
    """The entry service's declared version, if the sandbox reports one.

    Falls back to the *last* declared service when the sandbox does not name an
    entry service: service configs are stored in dependency-first order, so the
    last one is the caller and the first is the datastore whose version describes
    something else entirely.
    """
    entry = configs.get(entry_service) if entry_service else None
    if isinstance(entry, dict) and entry.get("app_version"):
        return str(entry["app_version"])
    for config in reversed(list(configs.values())):
        if isinstance(config, dict) and config.get("app_version"):
            return str(config["app_version"])
    return None


def _normalize(value: Any) -> Any:
    """Normalize for comparison: dicts by key order, numerics by value."""
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    return str(value) if value is not None else None


def _changed_keys(left: Any, right: Any) -> list[str]:
    """Keys whose values differ between two (possibly nested) mappings."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return []
    changed: list[str] = []
    for key in sorted(set(left) | set(right)):
        if _normalize(left.get(key)) != _normalize(right.get(key)):
            changed.append(str(key))
    return changed


def _merge_reports(
    first: SanitizationReport, second: SanitizationReport
) -> dict[str, Any]:
    merged = SanitizationReport(
        redactions=first.redactions + second.redactions,
        dropped_keys=first.dropped_keys + second.dropped_keys,
        pseudonyms=first.pseudonyms + second.pseudonyms,
        unresolved=first.unresolved + second.unresolved,
    )
    return merged.as_dict()


__all__ = [
    "EQUAL",
    "MAJOR",
    "MINOR",
    "EnvironmentSnapshotService",
    "SnapshotData",
]
