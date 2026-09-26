"""Process-local runtime metrics (hardening: self-observability).

Every other series on ``/metrics`` is derived from Postgres, because Postgres is
the source of truth about work done. A few facts only ever exist inside a
running process, though, and they are exactly the ones an operator needs when
the platform is misbehaving:

* which rate-limit backend this replica is actually using (a fleet where one
  replica silently fell back to per-process limiting does not enforce the
  documented ceiling, and nothing in the database would say so);
* how often that fallback happened;
* which instance and region produced this scrape — without it, a multi-region
  deployment cannot tell its replicas apart in a dashboard.

Two rules shape this module:

1. **It is never a source of truth about work.** Anything durable belongs in
   Postgres, so it survives a restart and is visible to every replica. These
   values are per-process and reset when the process does.
2. **A scrape never fails because a metric is missing.** Rendering is total:
   an unregistered name is simply absent.
"""

from __future__ import annotations

import os
import socket
import threading
from dataclasses import dataclass, field
from typing import Final, Optional

#: Metric names whose exposition this module owns. Kept explicit so a typo in a
#: call site is a missing series rather than a silently misspelled one.
_PREFIX: Final[str] = "argus_"


@dataclass
class _Family:
    """One metric family: its type, its help string, and its current values."""

    metric_type: str
    help_text: str
    values: dict[str, float] = field(default_factory=dict)


_LOCK = threading.Lock()
_FAMILIES: dict[str, _Family] = {}


def _family(name: str, metric_type: str, help_text: str) -> _Family:
    family = _FAMILIES.get(name)
    if family is None:
        family = _Family(metric_type=metric_type, help_text=help_text)
        _FAMILIES[name] = family
    return family


def incr(name: str, *, help_text: str, amount: float = 1.0) -> None:
    """Add to a counter. Monotonic by contract — never decremented."""
    if not name.startswith(_PREFIX):
        return
    with _LOCK:
        family = _family(name, "counter", help_text)
        family.values[""] = family.values.get("", 0.0) + amount


def gauge(name: str, value: float, *, help_text: str) -> None:
    """Set a gauge to its current value."""
    if not name.startswith(_PREFIX):
        return
    with _LOCK:
        family = _family(name, "gauge", help_text)
        family.values[""] = float(value)


def init_counter(name: str, *, help_text: str) -> None:
    """Register a counter at zero, without claiming an event happened.

    Why this exists rather than ``incr(..., amount=0)`` as a habit: a counter
    that only appears after its first event is a series a dashboard reads as
    *missing*, and a missing series and a zero series look identical to a naive
    ``rate()`` while meaning opposite things. Emitting zero from the start is
    the correct Prometheus semantics for a counter, and it lets an alert
    reference the series in a fresh deployment without firing on absence.
    """
    if not name.startswith(_PREFIX):
        return
    with _LOCK:
        _family(name, "counter", help_text).values.setdefault("", 0.0)


def instance_id() -> str:
    """Stable identity for this process, for per-instance dashboards.

    ``INSTANCE_ID`` (the setting's own name, so operators set it once) wins
    when present — a container name, a pod name, a host. Otherwise
    ``hostname:pid`` is specific enough to separate replicas sharing a host and
    needs no configuration to be useful.
    """
    configured = os.environ.get("INSTANCE_ID", "").strip()
    if configured:
        return configured
    return f"{socket.gethostname()}:{os.getpid()}"


def instance_region() -> str:
    """Deployment region/zone label. ``local`` for a single-site deployment."""
    return os.environ.get("INSTANCE_REGION", "").strip() or "local"


def render_prometheus(
    *,
    version: Optional[str] = None,
    instance: Optional[str] = None,
    region: Optional[str] = None,
) -> list[str]:
    """Render every registered family, plus the instance identity series.

    The identity series is always present, even when no counter has moved: a
    dashboard that cannot see which replicas exist is the failure this module
    was added to prevent. ``instance``/``region``/``version`` are supplied by
    the caller from :class:`~app.core.config.Settings`, so configuration has one
    source; the environment fallbacks keep this module usable on its own.
    """
    resolved_instance = instance or instance_id()
    resolved_region = region or instance_region()
    resolved_version = version or os.environ.get("ARGUS_VERSION") or "unknown"
    lines: list[str] = [
        "# HELP argus_instance_info Identity of the process serving this scrape",
        "# TYPE argus_instance_info gauge",
        (
            "argus_instance_info"
            f'{{instance="{_escape(resolved_instance)}",'
            f'region="{_escape(resolved_region)}",'
            f'version="{_escape(resolved_version)}"}} 1'
        ),
    ]
    with _LOCK:
        snapshot = [
            (name, family, dict(family.values))
            for name, family in sorted(_FAMILIES.items())
        ]
    for name, family, values in snapshot:
        if not values:
            continue
        lines.append(f"# HELP {name} {family.help_text}")
        lines.append(f"# TYPE {name} {family.metric_type}")
        for labels, value in sorted(values.items()):
            suffix = f"{{{labels}}}" if labels else ""
            lines.append(f"{name}{suffix} {_format_value(value)}")
    return lines


def snapshot() -> dict[str, float]:
    """Current values, keyed ``name`` (no metrics endpoint formatting).

    Used by tests and by the health surface, which should not have to parse the
    exposition format to ask one question.
    """
    out: dict[str, float] = {}
    with _LOCK:
        for name, family in _FAMILIES.items():
            if "" in family.values:
                out[name] = family.values[""]
    return out


def reset() -> None:
    """Clear every registered family. Tests only — never called in production."""
    with _LOCK:
        _FAMILIES.clear()


def _format_value(value: float) -> str:
    """Prometheus wants a number, not Python's repr of a float."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _escape(value: str) -> str:
    """Label values are quoted strings: backslash, quote and newline are escaped."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
