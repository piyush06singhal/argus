"""ARGUS Redaction Engine — secret-safe event payloads.

Phase 1 §46: the ingestion pipeline redacts sensitive fields from event payloads
before persistence. This is a defence-in-depth measure; the API schema also
rejects known secret keys at the boundary.

Design:
  - Pattern-based detection of secret values (long base64/hex strings, JWTs, etc.)
  - Key-name heuristics (password, token, secret, api_key, …)
  - Always returns a *new* dict — never mutates the caller's data.
  - Nested dicts and lists are recursively redacted.
"""
from __future__ import annotations

import re
from typing import Any

# Key names that indicate a sensitive field (case-insensitive match).
_SENSITIVE_KEY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(password|passwd|pwd)$"),
    re.compile(r"(?i)(secret|secret_key|secret_token)$"),
    re.compile(r"(?i)(api[_-]?key|apikey)$"),
    re.compile(r"(?i)(access[_-]?token|auth[_-]?token|bearer)$"),
    re.compile(r"(?i)(private[_-]?key|priv[_-]?key)$"),
    re.compile(r"(?i)(credit[_-]?card|card[_-]?number|cvv)$"),
    re.compile(r"(?i)(ssn|social[_-]?security)$"),
]

# Values that look like encoded secrets regardless of key name.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_LONG_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_LONG_HEX_RE = re.compile(r"[0-9a-fA-F]{32,}")

_REDACTED = "[REDACTED]"


class RedactionEngine:
    """Redact sensitive values from an event payload dict.

    Usage::

        engine = RedactionEngine()
        safe = engine.redact({"password": "s3cr3t", "user": "alice"})
        # safe == {"password": "[REDACTED]", "user": "alice"}
    """

    def redact(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        """Return a redacted copy of *payload*.

        Returns an empty dict when *payload* is ``None``.
        """
        if not payload:
            return {}
        return self._redact_value(payload)

    # -- internals -------------------------------------------------------

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self._redact_entry(k, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value

    def _redact_entry(self, key: str, value: Any) -> Any:
        if self._is_sensitive_key(key):
            return _REDACTED
        if isinstance(value, str) and self._looks_like_secret(value):
            return _REDACTED
        return self._redact_value(value)

    @staticmethod
    def _is_sensitive_key(key: str) -> bool:
        return any(p.search(key) for p in _SENSITIVE_KEY_PATTERNS)

    @staticmethod
    def _looks_like_secret(value: str) -> bool:
        if len(value) < 20:
            return False
        return bool(_JWT_RE.search(value) or _LONG_BASE64_RE.fullmatch(value) or _LONG_HEX_RE.fullmatch(value))

    def payload_summary(self, payload: dict[str, Any] | None, max_keys: int = 10) -> dict[str, Any]:
        """Return a redacted, key-only summary for dead-letter storage.

        Only key names and types are preserved — values are replaced with
        their type name, or ``[REDACTED]`` for sensitive keys.
        """
        if not payload:
            return {}
        summary: dict[str, Any] = {}
        for i, (k, v) in enumerate(payload.items()):
            if i >= max_keys:
                summary["..."] = f"{len(payload) - max_keys} more keys"
                break
            if self._is_sensitive_key(k):
                summary[k] = "[REDACTED]"
            elif isinstance(v, dict):
                summary[k] = "object"
            elif isinstance(v, list):
                summary[k] = f"array[{len(v)}]"
            else:
                summary[k] = type(v).__name__
        return summary
