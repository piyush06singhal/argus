"""ARGUS Source Secret Redaction (Phase 6 §57).

Redacts secrets from **source text and telemetry excerpts** before they are sent
to a model provider. This is a different job from Phase 1's
:class:`~app.services.redaction.RedactionEngine`, which redacts *structured
payloads by key name* (``{"password": ...}``). Source code carries secrets
*inline* — ``API_KEY = "sk-live-…"``, a connection string with a password, a
PEM block — so the unit of analysis here is text, and the detection is
value-shaped.

Two rules the implementation takes seriously:

* **The report never contains a secret.** It counts categories
  (``{"private_key_block": 1, "bearer_token": 2}``) and never echoes a matched
  value, a prefix of it, or its position in a way that would let a reader
  reconstruct it. A redaction log that quotes what it redacted is a second leak.
* **Redaction errs toward removing.** A false positive costs a little context; a
  false negative ships a production credential to a third party. Where a pattern
  is ambiguous the placeholder is longer, not shorter.

Nothing here is a security boundary on its own — it is one layer of a defence
that also includes never sending whole repositories, scoping every read, and
treating all repository text as untrusted data (§58).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

REDACTED = "[REDACTED:SECRET]"
REDACTED_KEY = "[REDACTED:PRIVATE_KEY]"

#: Values that match a secret-shaped *name* but are not secrets. Redacting these
#: would tell a reader less than the code does: ``password = None`` is information
#: (the field is unset), and replacing it with a placeholder invents a secret that
#: does not exist.
NON_SECRET_LITERALS = frozenset(
    {
        "none",
        "null",
        "nil",
        "undefined",
        "true",
        "false",
        "empty",
        "changeme",
        "change-me",
        "placeholder",
        "example",
        "dummy",
        "redacted",
        "xxx",
        "***",
        "env",
        "getenv",
        "os.environ",
    }
)

#: PEM-style private key blocks. Matched first and by block, because a key body
#: has no internal marker that a value-level pattern could key on.
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

#: ``NAME = "value"`` / ``NAME: value`` where the name says "secret". The quote
#: is captured so the replacement can close it and leave the line syntactically
#: intact — mangled code would defeat the point of sending the excerpt at all.
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)        (?P<prefix>
        (?P<name>
            #: The leading class allows *zero* characters before the keyword, so a
            #: name that IS the keyword (``password``, ``token``) matches. An
            #: earlier version required a leading character and therefore silently
            #: missed exactly the most obvious names.
            [A-Za-z0-9_.\-]*
            (secret|password|passwd|pwd|api[_-]?key|apikey|
             access[_-]?token|auth[_-]?token|private[_-]?key|credential|
             client[_-]?secret|bearer|signing[_-]?key|encryption[_-]?key)
            [A-Za-z0-9_.\-]*
        )
        \s* [:=] \s* (?P<quote>['\"]?)
    )
    (?P<value>[^\s'\"]{4,})
    """,
)

#: ``key=value`` and ``key: value`` inside connection strings / URLs.
_URL_CREDENTIAL = re.compile(
    r"(?i)\b(?P<scheme>[a-z][a-z0-9+.\-]{1,20})://(?P<user>[^:/@\s]+):(?P<secret>[^@/\s]+)@"
)

#: Known provider token shapes. Anchored on real prefixes so a random hex string
#: is not automatically treated as a key (that would redact every commit sha).
_PROVIDER_TOKENS = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),  # OpenAI-style
    re.compile(r"\bsk_live_[A-Za-z0-9]{16,}\b"),  # Stripe
    re.compile(r"\bsk_test_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),  # GitHub PAT
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),  # Google API key
    re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}\b"),  # Google OAuth
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}\b"),  # GitLab PAT
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"),
)

#: ``Authorization: Bearer <token>``.
_BEARER = re.compile(r"(?i)\b(bearer|token)\s+([A-Za-z0-9._\-]{12,})")

#: JWTs: three base64url segments separated by dots, starting with the ``eyJ``
#: header marker. Requires all three segments so an ordinary dotted identifier
#: is left alone.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")

#: Long unbroken base64/hex blobs. Length is the heuristic: a 40-character run
#: of one alphabet is almost never a literal a human wrote on purpose.
#:
#: Hex is held to 64 characters, not 40, and that number is load-bearing: a
#: 40-character hex string is a git commit sha or a sha1 blob id, both of which
#: appear in the very evidence this phase cites. Redacting them would invalidate
#: the commit references the analysis is built on, which is a correctness bug, not
#: a privacy win.
_LONG_BLOB = re.compile(r"\b(?P<value>[A-Za-z0-9+/]{40,}={0,2}|[0-9a-fA-F]{64,})\b")

#: Keys whose ``.env``-style line is always sensitive, even when short.
_ENV_LINE = re.compile(
    r"(?im)^(?P<name>[A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|KEY|CREDENTIAL)[A-Z0-9_]*)=(?P<value>.+)$"
)


@dataclass
class RedactionReport:
    """What was removed. Contains **counts only** — never a matched value."""

    counts: dict = field(default_factory=dict)
    characters_removed: int = 0

    def record(self, category: str, removed_chars: int) -> None:
        self.counts[category] = self.counts.get(category, 0) + 1
        self.characters_removed += removed_chars

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def as_dict(self) -> dict:
        return {
            "categories": dict(self.counts),
            "total_redacted": self.total,
            "characters_removed": self.characters_removed,
        }


class SourceRedactor:
    """Removes secret-shaped values from text, with an auditable count."""

    def redact(self, text: Optional[str]) -> tuple[str, RedactionReport]:
        """Return ``(redacted_text, report)``.

        The order of passes matters: private key blocks first (their bodies would
        otherwise be shredded by the generic blob rule, losing the block
        structure), then named assignments, then provider tokens, then the
        generic long-blob heuristic (which is the most likely to over-match).
        """
        report = RedactionReport()
        if not text:
            return "", report
        result = text

        result = _PRIVATE_KEY_BLOCK.sub(
            lambda m: self._plain(
                report, "private_key_block", m.group(0), REDACTED_KEY
            ),
            result,
        )
        result = _SECRET_ASSIGNMENT.sub(lambda m: self._assignment(report, m), result)
        result = _URL_CREDENTIAL.sub(lambda m: self._url_credential(report, m), result)
        result = _ENV_LINE.sub(lambda m: self._env_line(report, m), result)
        result = _JWT.sub(
            lambda m: self._plain(report, "jwt", m.group(0), REDACTED), result
        )
        result = _BEARER.sub(lambda m: self._bearer(report, m), result)
        for pattern in _PROVIDER_TOKENS:
            result = pattern.sub(
                lambda m: self._plain(report, "provider_token", m.group(0), REDACTED),
                result,
            )
        result = _LONG_BLOB.sub(lambda m: self._blob(report, m), result)
        return result, report

    def redact_many(
        self, texts: dict[str, Optional[str]]
    ) -> tuple[dict[str, str], RedactionReport]:
        """Redact a mapping, merging every report into one."""
        combined = RedactionReport()
        output: dict[str, str] = {}
        for key, value in texts.items():
            cleaned, report = self.redact(value)
            output[key] = cleaned
            for category, count in report.counts.items():
                combined.counts[category] = combined.counts.get(category, 0) + count
            combined.characters_removed += report.characters_removed
        return output, combined

    # -- internals ---------------------------------------------------------
    #: Each replacer keeps the *identifying* part of a match (the key name, the
    #: URL scheme and user, the ``Bearer`` keyword) and replaces only the secret,
    #: so the excerpt still parses and still reads as the code it came from.
    @staticmethod
    def _plain(
        report: RedactionReport, category: str, matched: str, replacement: str
    ) -> str:
        report.record(category, max(len(matched) - len(replacement), 0))
        return replacement

    def _assignment(self, report: RedactionReport, match: re.Match) -> str:
        value = match.group("value") or ""
        if value.strip().strip("'\"").lower() in NON_SECRET_LITERALS:
            report.counts["not_a_secret_skipped"] = (
                report.counts.get("not_a_secret_skipped", 0) + 1
            )
            return match.group(0)
        prefix = match.group("prefix") or ""
        #: No closing quote is appended: the value class stops *before* the
        #: closing quote, so the original one is still in the text and appending
        #: another produced ``"[REDACTED]""`` — visible damage in the excerpt.
        replacement = f"{prefix}{REDACTED}"
        report.record(
            "secret_assignment", max(len(match.group(0)) - len(replacement), 0)
        )
        return replacement

    def _blob(self, report: RedactionReport, match: re.Match) -> str:
        """Redact a long encoded blob, unless it is a git object id.

        A 40-character hex run is a commit sha, a blob id or a tree id — all of
        which appear in the evidence this phase cites and must survive. The
        regex cannot express "hex but short" cleanly, so the decision is made
        here where it is readable and testable.
        """
        value = match.group(0)
        if value.isalnum() and all(char in "0123456789abcdefABCDEF" for char in value):
            if len(value) < 64:
                report.counts["git_object_id_skipped"] = (
                    report.counts.get("git_object_id_skipped", 0) + 1
                )
                return value
        return self._plain(report, "long_encoded_blob", value, REDACTED)

    def _url_credential(self, report: RedactionReport, match: re.Match) -> str:
        replacement = f"{match.group('scheme')}://{match.group('user')}:{REDACTED}@"
        report.record("url_credential", max(len(match.group(0)) - len(replacement), 0))
        return replacement

    def _env_line(self, report: RedactionReport, match: re.Match) -> str:
        replacement = f"{match.group('name')}={REDACTED}"
        report.record("env_assignment", max(len(match.group(0)) - len(replacement), 0))
        return replacement

    def _bearer(self, report: RedactionReport, match: re.Match) -> str:
        keyword = match.group(1)
        replacement = f"{keyword} {REDACTED}"
        report.record("bearer_token", max(len(match.group(0)) - len(replacement), 0))
        return replacement


#: Shared default instance: the redactor is stateless.
default_redactor = SourceRedactor()


def redact_source(text: Optional[str]) -> tuple[str, RedactionReport]:
    """Convenience wrapper for the shared redactor."""
    return default_redactor.redact(text)
