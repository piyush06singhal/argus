"""Documentation-consistency guard (production-hardening pass).

The hardening audit found documentation that had drifted from the code: stale
counts, a config default stated backwards, and a "no authentication yet" that was
flatly untrue. A doc that overstates the platform is worse than no doc, so the
audit's findings are turned into assertions here.

This module checks only invariants that are *derivable from the repository* — a
claim nobody can check by hand is a claim that will rot:

* **Every alert's runbook link resolves.** Each rule in ``alerts.yml`` names a
  section of ``docs/operations.md``. An anchor that matches no heading is a
  runbook that sends an on-call engineer to a page that does not exist.
* **Every alert name in prose is a real alert.** A rule renamed in ``alerts.yml``
  but left under its old name in the runbook is worse than a missing entry: the
  reader looks up a string the system never emits.
* **Every alert has a runbook entry.** The converse direction, so a new rule
  cannot ship without somewhere to send its reader.
* **Every live gate is named in the README.** ``verify-all.sh`` drives a set of
  ``infrastructure/e2e-*.sh`` gates; a gate that exists but is undocumented is a
  gate a reader cannot run.
* **The advertised alert-rule count matches the file.** A number in prose that no
  longer counts what it says.
* **Every live gate is wired into `verify-all.sh`.** A gate nobody runs is a gate
  that will rot, and "it exists" is not the same claim as "it runs in CI".
* **The advertised live-gate count matches the harness.** The same drift, on the
  other number these docs advertise.
* **No doc denies a capability the code ships.** This is the guard against the
  most damaging kind of doc bug: a false statement that a shipped feature (OTLP
  Protobuf decoding, federated sign-in) does not exist.

The tests read files, never the network, so they run in the default suite.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ALERTS = REPO_ROOT / "infrastructure" / "observability" / "prometheus" / "alerts.yml"
OPERATIONS = REPO_ROOT / "docs" / "operations.md"
README = REPO_ROOT / "README.md"
GATES_DIR = REPO_ROOT / "infrastructure"
DOCS_DIR = REPO_ROOT / "docs"
MAIN = REPO_ROOT / "apps" / "api" / "app" / "main.py"
VERIFY_ALL = GATES_DIR / "verify-all.sh"
#: Documents that advertise *current* counts. The historical audit snapshots
#: (``PRODUCTION-HARDENING-PLAN.md``, the per-phase reports) are kept verbatim on
#: purpose — they describe the system as it was when the audit ran, so pinning
#: their numbers to today's would rewrite the evidence.
CURRENT_CLAIMS = [README, DOCS_DIR / "production-readiness.md"]

#: Docs whose prose names alert rules. The runbook is the one that must; the
#: README may mention a few, and any it mentions must be real too.
DOCS_NAMED_HERE = [OPERATIONS, README]

_ALERT_NAME = re.compile(r"\balert:\s*([A-Za-z][A-Za-z0-9_]*)\s*$")
_RUNBOOK_URL = re.compile(r'runbook_url:\s*"([^"]+)"')
#: An alert identifier in prose: CamelCase, `Argus`-prefixed. Deliberately narrow
#: — it must not match metric names (``argus_...``) or the product name.
_ALERT_TOKEN = re.compile(r"\bArgus[A-Z][A-Za-z0-9]*\b")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _alert_names() -> list[str]:
    names = [
        match.group(1)
        for line in _read(ALERTS).splitlines()
        if (match := _ALERT_NAME.search(line))
    ]
    assert names, "no alert rules parsed from alerts.yml — did the format change?"
    return names


def _alert_runbook_links() -> list[str]:
    return _RUNBOOK_URL.findall(_read(ALERTS))


def _slugify(heading: str) -> str:
    """The anchor GitHub generates for a heading.

    Lowercase; drop everything that is not a word character, whitespace or a
    hyphen; collapse whitespace to hyphens. Enough for every heading this
    repository uses, and the reason ``backups-and-recovery`` resolves.
    """
    text = heading.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text)


def _document_anchors(path: Path) -> set[str]:
    return {
        _slugify(line.lstrip("#").strip())
        for line in _read(path).splitlines()
        if line.startswith("#") and line.lstrip("#").strip()
    }


def test_every_runbook_link_resolves_to_a_real_section() -> None:
    anchors = _document_anchors(OPERATIONS)
    broken: dict[str, str] = {}
    for link in _alert_runbook_links():
        _, _, anchor = link.partition("#")
        assert anchor, f"runbook_url without an anchor: {link!r}"
        if anchor not in anchors:
            broken[link] = anchor
    assert not broken, (
        "alert runbook links point at sections that do not exist in "
        f"docs/operations.md: {broken}"
    )


def test_every_alert_rule_links_a_runbook() -> None:
    names = _alert_names()
    links = _alert_runbook_links()
    assert len(links) == len(names), (
        f"{len(names)} alert rules but {len(links)} runbook links — every rule "
        "must send its reader somewhere"
    )


def test_alert_names_in_prose_are_real_alerts() -> None:
    known = set(_alert_names())
    invented: dict[str, list[str]] = {}
    for path in DOCS_NAMED_HERE:
        for token in sorted(set(_ALERT_TOKEN.findall(_read(path)))):
            if token not in known:
                invented.setdefault(str(path.relative_to(REPO_ROOT)), []).append(token)
    assert not invented, (
        "documentation names alert rules that do not exist in alerts.yml "
        f"(wrong name, or a renamed rule): {invented}"
    )


def test_every_alert_has_a_runbook_entry() -> None:
    known = set(_alert_names())
    documented: set[str] = set()
    for path in DOCS_NAMED_HERE:
        documented.update(_ALERT_TOKEN.findall(_read(path)))
    missing = sorted(known - documented)
    assert not missing, (
        "these alert rules have no entry in the runbook docs, so an on-call "
        f"engineer has nowhere to look: {missing}"
    )


#: A gate spec in ``verify-all.sh``: ``"label|stem"`` in the spec list.
_GATE_SPEC = re.compile(r'\|\s*([a-z0-9-]+)"')

#: Gates deliberately outside the ``verify-all.sh`` matrix, with the reason.
_NOT_IN_VERIFY_ALL = {
    "e2e-smoke.sh": (
        "the Phase 0 smoke builds a whole seeded lifecycle; the phase gates "
        "supersede it and it stays runnable by hand"
    ),
}


def _live_gate_scripts() -> list[Path]:
    return sorted(GATES_DIR.glob("e2e-*.sh"))


def _verify_all_stems() -> list[str]:
    stems = _GATE_SPEC.findall(_read(VERIFY_ALL))
    assert stems, "no gate specs parsed from verify-all.sh — did the format change?"
    return stems


def test_every_live_gate_is_documented() -> None:
    readme = _read(README)
    undocumented = [
        script.name for script in _live_gate_scripts() if script.name not in readme
    ]
    assert not undocumented, (
        "these live gates exist but the README never names them, so a reader "
        f"cannot run them: {undocumented}"
    )


def test_every_live_gate_is_wired_into_verify_all() -> None:
    """A gate that nothing runs is not a gate.

    ``verify-all.sh`` is the one command the docs tell an operator to run, so a
    gate missing from its matrix is a proof nobody executes — exactly the state
    this repository keeps finding and closing.
    """
    harness = _read(VERIFY_ALL)
    unwired: list[str] = []
    for script in _live_gate_scripts():
        if script.name in _NOT_IN_VERIFY_ALL:
            continue
        if script.name in harness or script.stem in harness:
            continue
        unwired.append(script.name)
    assert not unwired, (
        "these live gates exist but verify-all.sh never runs them, so they are "
        f"not part of the validation matrix: {unwired}"
    )


def test_the_advertised_live_gate_count_is_true() -> None:
    """If prose says "N live gates", the harness had better drive N gates."""
    count = len(_verify_all_stems())
    pattern = re.compile(r"(\d+)\s+live gates\b")
    checked = 0
    for path in CURRENT_CLAIMS:
        for claimed in pattern.findall(_read(path)):
            checked += 1
            assert int(claimed) == count, (
                f"{path.relative_to(REPO_ROOT)} advertises {claimed} live gates "
                f"but verify-all.sh drives {count}"
            )
    assert checked > 0, (
        "no 'N live gates' claim found to verify; if the claim was removed, "
        "remove this test rather than leaving it vacuous"
    )


def test_no_doc_denies_single_sign_on_the_code_ships() -> None:
    """A doc that says SSO is not provided is false when the code ships it.

    The hardening audit found exactly this: ``security-architecture.md`` listed
    "SSO/OIDC or user accounts" under "what is deliberately not provided" while
    ``app/services/oidc.py`` and the ``/auth/oidc`` routes shipped a complete
    authorization-code flow. Like the Protobuf guard below, this checks the
    capability first: remove the service and the denial becomes true again.
    """
    if not (REPO_ROOT / "apps" / "api" / "app" / "services" / "oidc.py").exists():
        return  # SSO removed — the denial would be correct again
    denials = (
        re.compile(r"SSO/OIDC or user accounts", re.IGNORECASE),
        re.compile(r"no SSO\b", re.IGNORECASE),
        re.compile(
            r"\bSSO\b[^\n]{0,80}\bnot\s+(?:currently\s+)?"
            r"(?:ship|shipped|provide|provided|support|supported|available)",
            re.IGNORECASE,
        ),
    )
    offenders: dict[str, str] = {}
    for path in _current_prose_documents():
        text = _read(path)
        for pattern in denials:
            match = pattern.search(text)
            if match:
                offenders[str(path.relative_to(REPO_ROOT))] = match.group(0)
    assert not offenders, (
        "these docs deny single sign-on, but app/services/oidc.py ships the "
        f"authorization-code flow: {offenders}"
    )


def _prose_documents() -> list[Path]:
    return sorted(DOCS_DIR.glob("*.md")) + [README]


#: Audit snapshots that describe the platform as it was and are kept verbatim.
#: A capability guard must not read them as a claim about the current system —
#: "no SSO" was true when that plan was written and is preserved on purpose.
_HISTORICAL = re.compile(
    r"(?:phase-\d+-report|final-architecture-audit|" r"PRODUCTION-HARDENING-PLAN)\.md$"
)


def _current_prose_documents() -> list[Path]:
    return [p for p in _prose_documents() if not _HISTORICAL.search(p.name)]


def test_no_doc_denies_a_capability_the_code_ships() -> None:
    """A doc that says "Protobuf is not accepted" is false when the code accepts it.

    ``OtlpProtobufMiddleware`` decodes ``application/x-protobuf`` OTLP exports,
    so the onboarding guide's old "JSON transport only" was a lie about the
    platform. If the middleware is ever removed the denial becomes true again,
    which is why this checks the marker before policing prose.
    """
    if "OtlpProtobufMiddleware" not in _read(MAIN):
        return  # middleware removed — the denial would be correct again
    denial = re.compile(r"[Pp]rotobuf is not accepted")
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _prose_documents()
        if denial.search(_read(path))
    ]
    assert not offenders, (
        "these docs claim OTLP/Protobuf is not accepted, but app/main.py "
        f"registers OtlpProtobufMiddleware, which decodes it: {offenders}"
    )


def test_the_advertised_alert_rule_count_is_true() -> None:
    """If prose says "N alert rules", the file had better hold N rules."""
    count = len(_alert_names())
    pattern = re.compile(r"(\d+)\s+alert\s+rules\b")
    checked = 0
    for path in DOCS_NAMED_HERE:
        text = _read(path)
        for claimed in pattern.findall(text):
            checked += 1
            assert int(claimed) == count, (
                f"{path.relative_to(REPO_ROOT)} advertises {claimed} alert rules "
                f"but alerts.yml defines {count}"
            )
    # Not an assertion on the number itself — only that we are checking one.
    assert checked > 0, (
        "no 'N alert rules' claim found to verify; if the claim was removed, "
        "remove this test rather than leaving it vacuous"
    )
