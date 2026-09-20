"""ARGUS Reproduction Sanitizers (Phase 5 §15, §16).

Everything a sandbox sees passes through here first: environment variables,
configuration snapshots, and every replay payload. Three commitments:

**Fail safe, not best-effort.** A value under a secret-named key is *tainted*:
every scalar in that subtree becomes a placeholder, so a nested credential blob
cannot survive as a leaf nobody thought to check. The only hard stop is a
structure too deep to verify (see :class:`SanitizationError`) — an experiment
that cannot be proven safe does not run (§15).

**Structure survives, identity does not.** Real identifiers are replaced with
*deterministic* pseudonyms — the same ``user_id`` maps to the same synthetic
UUID in every run, so reproductions stay comparable and repeatable, while the
original value is unrecoverable from what is stored. PII is *replaced*, never
deleted, so a request keeps its shape (§16).

**Nothing here writes an original value anywhere.** Redaction records contain a
path and a kind — never the secret that was found.

:meth:`_SanitizeWalker.assert_sanitized` is a genuinely independent pre-flight
check used by the replay engine immediately before transmitting: it re-derives
"is anything sensitive still here?" from the stored payload instead of trusting
that sanitization happened (§18).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.services.redaction import RedactionEngine

#: Namespace for deterministic pseudonyms. A fixed namespace keeps pseudonyms
#: stable across runs (a prerequisite for repeatable experiments) while making
#: them independent of the original value's format.
PSEUDONYM_NAMESPACE = uuid.UUID("8b1c5f2e-9d3a-4c7b-8e5f-1a2b3c4d5e6f")

REDACTED = "[REDACTED]"

#: Maximum nesting the walker will descend before refusing. Deeper structures
#: cannot be *proven* free of secrets by inspection, so they are rejected.
MAX_SANITIZE_DEPTH = 12

#: Keys whose values are replaced by a deterministic pseudonym rather than a
#: placeholder, because experiments need identifiable-but-fake subjects.
_PSEUDONYM_KEY_RE = re.compile(
    r"(?i)^(user_?id|customer_?id|account_?id|tenant_?id|session_?id|subject|sub)$"
)

#: Keys whose values are identity-ish free text (replaced with a placeholder).
_PII_TEXT_KEY_RE = re.compile(
    r"(?i)(email|e_?mail|phone|mobile|msisdn|address|street|postal|zip|"
    r"full_?name|first_?name|last_?name|surname|dob|date_?of_?birth|"
    r"passport|national_?id|tax_?id|iban|card|credit_?card|cvv|ssn)"
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)")
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
#: Provider-style external identifiers (Stripe-like, GitHub-like, AWS-like).
_EXTERNAL_ID_RE = re.compile(
    r"\b(?:acct|cus|sub|pi|pm|ch|card|inv|tok|sk|pk|xoxb|AKIA|ghp)"
    r"_[A-Za-z0-9]{6,}\b"
)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}")

#: Environment-variable names that must never enter a sandbox with a real value.
SECRET_KEY_RE = re.compile(
    r"(?i)(secret|password|passwd|pwd|token|api[_-]?key|apikey|"
    r"private[_-]?key|credential|auth|access[_-]?key|session[_-]?key)"
)

#: Environment-variable names a sandbox is allowed to inherit. Everything else
#: is dropped rather than forwarded: a sandbox does not need the host's config.
ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "LANG",
        "LC_ALL",
        "TZ",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "PATH",
    }
)

#: Sandbox-safe substitutes for secret env vars a service genuinely needs to
#: boot. These are obviously fake, and grant access to nothing.
SECRET_SUBSTITUTES: dict[str, str] = {
    "DATABASE_URL": "postgresql://sandbox:sandbox@127.0.0.1:1/sandbox_none",
    "REDIS_URL": "redis://127.0.0.1:1/0",
    "API_KEY": "sandbox-not-a-real-key",
}


class SanitizationError(ValueError):
    """Raised when a value cannot be *proven* safe to place in a sandbox.

    This is a hard stop: the reproduction engine refuses to execute rather than
    guessing. A refused experiment is an honest outcome; a leaked credential is
    not recoverable.
    """


@dataclass
class SanitizationReport:
    """A record of what was replaced — deliberately value-free."""

    redactions: list[dict[str, Any]] = field(default_factory=list)
    dropped_keys: list[str] = field(default_factory=list)
    pseudonyms: int = 0
    unresolved: list[str] = field(default_factory=list)

    def record(self, path: str, kind: str) -> None:
        self.redactions.append({"path": path, "kind": kind})

    def as_dict(self) -> dict[str, Any]:
        return {
            "redaction_count": len(self.redactions),
            "dropped_key_count": len(self.dropped_keys),
            "pseudonym_count": self.pseudonyms,
            "unresolved_count": len(self.unresolved),
            "kinds": sorted({item["kind"] for item in self.redactions}),
            "dropped_keys": self.dropped_keys[:50],
        }


def pseudonym(value: Any, *, prefix: str = "sub") -> str:
    """Deterministic, non-reversible stand-in for an identifier.

    ``uuid5`` over a fixed namespace means the same input always produces the
    same output — so a replayed request refers to the *same* synthetic subject
    on every repetition — while the original is not recoverable.
    """
    return f"{prefix}-{uuid.uuid5(PSEUDONYM_NAMESPACE, str(value))}"


class _SanitizeWalker:
    """Recursive redaction shared by payload and configuration handling.

    Subclasses tune behavior with two flags rather than re-implementing the
    walk, so the *safety* rules cannot diverge between the two call sites.
    """

    #: Replace identifier-shaped values with stable pseudonyms (payloads only).
    pseudonymize: bool = True
    #: Replace PII-shaped values with placeholders (payloads only).
    replace_pii: bool = True
    #: Human-readable name used in error messages.
    label: str = "payload"

    def __init__(self) -> None:
        self._engine = RedactionEngine()

    # -- classification --------------------------------------------------
    @staticmethod
    def _is_pseudonym_key(key: str) -> bool:
        return bool(_PSEUDONYM_KEY_RE.match(key))

    @staticmethod
    def _is_pii_text_key(key: str) -> bool:
        return bool(_PII_TEXT_KEY_RE.search(key))

    @staticmethod
    def _is_secret_key(key: str) -> bool:
        return bool(SECRET_KEY_RE.search(key)) or bool(
            RedactionEngine._is_sensitive_key(key)
        )

    # -- text ------------------------------------------------------------
    def sanitize_text(self, value: str) -> tuple[str, list[str]]:
        """Scrub embedded identifiers inside a free-text string."""
        kinds: list[str] = []
        result = value
        if _EMAIL_RE.search(result):
            result = _EMAIL_RE.sub("subject@sanitized.invalid", result)
            kinds.append("EMAIL")
        if _SSN_RE.search(result):
            result = _SSN_RE.sub("000-00-0000", result)
            kinds.append("SSN")
        if _CARD_RE.search(result):
            result = _CARD_RE.sub("4000000000000000", result)
            kinds.append("PAYMENT_CARD")
        if _EXTERNAL_ID_RE.search(result):
            result = _EXTERNAL_ID_RE.sub(
                lambda m: pseudonym(m.group(0), prefix="ext"), result
            )
            kinds.append("EXTERNAL_ID")
        if _JWT_RE.search(result):
            result = _JWT_RE.sub(REDACTED, result)
            kinds.append("JWT")
        if _PHONE_RE.search(result):
            result = _PHONE_RE.sub("+10000000000", result)
            kinds.append("PHONE")
        return result, kinds

    # -- subtree redaction (taint) ---------------------------------------
    def _redact_subtree(
        self, value: Any, path: str, report: SanitizationReport, *, depth: int
    ) -> Any:
        """Replace every scalar in a tainted subtree, preserving its shape.

        A secret-named key may hold a nested object (``auth: {token: ...}``).
        Rather than refusing the whole payload — which would make ordinary
        request shapes unusable — the entire subtree is flattened to
        placeholders: keys and list lengths survive (so the request keeps its
        structure), values do not.
        """
        if depth > MAX_SANITIZE_DEPTH:
            raise SanitizationError(
                f"{self.label} nesting deeper than {MAX_SANITIZE_DEPTH} levels "
                f"at {path}; refusing to sanitize a structure this deep"
            )
        if isinstance(value, dict):
            return {
                key: self._redact_subtree(
                    item, f"{path}.{key}", report, depth=depth + 1
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                self._redact_subtree(item, f"{path}[{i}]", report, depth=depth + 1)
                for i, item in enumerate(value)
            ]
        report.record(path, "SECRET_SUBTREE")
        return REDACTED

    # -- walking ---------------------------------------------------------
    def _walk(
        self,
        value: Any,
        path: str,
        report: SanitizationReport,
        *,
        depth: int,
        tainted: bool = False,
    ) -> Any:
        if depth > MAX_SANITIZE_DEPTH:
            raise SanitizationError(
                f"{self.label} nesting deeper than {MAX_SANITIZE_DEPTH} levels "
                f"at {path}; refusing to sanitize a structure this deep"
            )
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, item in value.items():
                child = f"{path}.{key}"
                key_str = str(key)
                if tainted:
                    out[key] = self._redact_subtree(
                        item, child, report, depth=depth + 1
                    )
                    continue
                if self._is_secret_key(key_str):
                    if isinstance(item, (dict, list)) and item:
                        out[key] = self._redact_subtree(
                            item, child, report, depth=depth + 1
                        )
                        report.record(child, "SECRET_SUBTREE")
                    else:
                        out[key] = REDACTED
                        report.record(child, "SECRET")
                    continue
                if (
                    self.pseudonymize
                    and self._is_pseudonym_key(key_str)
                    and isinstance(item, (str, int))
                ):
                    out[key] = pseudonym(item)
                    report.pseudonyms += 1
                    report.record(child, "IDENTIFIER_PSEUDONYM")
                    continue
                if (
                    self.replace_pii
                    and self._is_pii_text_key(key_str)
                    and isinstance(item, str)
                ):
                    out[key] = self._placeholder_for(key_str)
                    report.record(child, "PII")
                    continue
                out[key] = self._walk(item, child, report, depth=depth + 1)
            return out
        if isinstance(value, list):
            return [
                self._walk(item, f"{path}[{i}]", report, depth=depth + 1)
                for i, item in enumerate(value)
            ]
        if isinstance(value, str):
            scrubbed, kinds = self.sanitize_text(value)
            for kind in kinds:
                report.record(path, kind)
            if (
                not kinds
                and len(value) >= 20
                and self._engine._looks_like_secret(value)
            ):
                report.record(path, "SECRET_VALUE")
                return REDACTED
            return scrubbed
        return value

    # -- verification ----------------------------------------------------
    def assert_sanitized(self, value: Optional[dict[str, Any]]) -> list[str]:
        """Independent pre-flight check: is anything sensitive still present?

        Returns a list of findings; an empty list means the structure is safe to
        place in a sandbox. Used by the replay engine immediately before
        transmitting, so a payload that reached storage through any path at all
        is re-verified rather than trusted (§18).
        """
        findings: list[str] = []
        if not value:
            return findings
        self._scan(value, self.label, findings, depth=0, tainted=False)
        return findings

    def _scan(
        self,
        value: Any,
        path: str,
        findings: list[str],
        *,
        depth: int,
        tainted: bool,
    ) -> None:
        if depth > MAX_SANITIZE_DEPTH:
            findings.append(f"{path}: nesting too deep to verify")
            return
        if isinstance(value, dict):
            for key, item in value.items():
                child = f"{path}.{key}"
                secret = tainted or self._is_secret_key(str(key))
                if secret and not isinstance(item, (dict, list)):
                    if item != REDACTED:
                        findings.append(f"{child}: unreplaced secret value")
                    continue
                self._scan(item, child, findings, depth=depth + 1, tainted=secret)
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                self._scan(
                    item,
                    f"{path}[{index}]",
                    findings,
                    depth=depth + 1,
                    tainted=tainted,
                )
            return
        if tainted:
            if value != REDACTED:
                findings.append(f"{path}: unreplaced value under a secret key")
            return
        if isinstance(value, str) and len(value) >= 20:
            if self._engine._looks_like_secret(value) or _JWT_RE.search(value):
                findings.append(f"{path}: value looks like a secret")

    @staticmethod
    def _placeholder_for(key: str) -> str:
        lowered = key.lower()
        if "email" in lowered or "mail" in lowered:
            return "subject@sanitized.invalid"
        if "phone" in lowered or "mobile" in lowered or "msisdn" in lowered:
            return "+10000000000"
        if "card" in lowered or "cvv" in lowered:
            return "4000000000000000"
        if "iban" in lowered:
            return "GB00SANDBOX00000000000000"
        return "SANDBOX-VALUE"


class InputSanitizer(_SanitizeWalker):
    """Sanitize replay payloads before they are stored or transmitted (§16).

    Applies, in order: secret-key redaction (with taint propagation), identifier
    pseudonymisation, PII replacement, and embedded-value scrubbing
    (emails/phones/cards/SSNs/JWTs/external ids inside otherwise innocent
    strings).
    """

    label = "payload"

    def sanitize_payload(
        self, payload: Optional[dict[str, Any]]
    ) -> tuple[dict[str, Any], SanitizationReport]:
        """Return ``(sanitized_payload, report)`` — never mutating the input."""
        report = SanitizationReport()
        if not payload:
            return {}, report
        sanitized = self._walk(dict(payload), "payload", report, depth=0)
        return sanitized, report


class ConfigSanitizer(_SanitizeWalker):
    """Sanitize environment variables and configuration for a sandbox (§15).

    Configuration documents are walked with the same safety rules but without
    identity handling: a config's ``customer_id`` is a setting, not a subject,
    so it is left intact (and redacted only if its *key* is secret-named).

    Environment variables get a different treatment because they *become the
    process environment*: anything not on the allowlist is dropped, and a
    secret-named variable is replaced with an obviously-fake sandbox value when
    the service needs the key to exist to boot.
    """

    label = "config"
    pseudonymize = False
    replace_pii = False

    def sanitize_env(
        self, env: dict[str, str]
    ) -> tuple[dict[str, str], SanitizationReport]:
        """Return the *minimal* environment a sandbox may inherit."""
        report = SanitizationReport()
        out: dict[str, str] = {}
        for key, value in env.items():
            # The substitute map is consulted *before* the secret-name heuristic:
            # a variable like ``DATABASE_URL`` carries credentials without having
            # a secret-sounding name, and a declared sandbox-safe substitute is
            # the whole point of listing it.
            substitute = SECRET_SUBSTITUTES.get(key.upper())
            if substitute is not None:
                out[key] = substitute
                report.record(f"env.{key}", "SECRET_SUBSTITUTED")
                continue
            if self._is_secret_key(key):
                report.record(f"env.{key}", "SECRET_DROPPED")
                report.dropped_keys.append(key)
                continue
            if key not in ENV_ALLOWLIST:
                report.dropped_keys.append(key)
                continue
            out[key] = value
        # A sandbox must not inherit a proxy pointing at the host's network.
        for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            out.pop(proxy, None)
        return out, report

    def sanitize_config(
        self, config: Optional[dict[str, Any]]
    ) -> tuple[dict[str, Any], SanitizationReport]:
        """Walk a configuration document, replacing every secret value."""
        report = SanitizationReport()
        if not config:
            return {}, report
        return self._walk(dict(config), "config", report, depth=0), report

    def assert_no_secrets(self, config: Optional[dict[str, Any]]) -> list[str]:
        """Findings for any secret still present — a pre-flight safety check."""
        return self.assert_sanitized(config)


def sanitize_services_for_log(services: Iterable[str]) -> list[str]:
    """Normalize a list of sandbox service names for storage/reporting."""
    return sorted({name.strip() for name in services if name and name.strip()})


__all__ = [
    "ConfigSanitizer",
    "ENV_ALLOWLIST",
    "InputSanitizer",
    "MAX_SANITIZE_DEPTH",
    "PSEUDONYM_NAMESPACE",
    "REDACTED",
    "SECRET_KEY_RE",
    "SECRET_SUBSTITUTES",
    "SanitizationError",
    "SanitizationReport",
    "pseudonym",
    "sanitize_services_for_log",
]
