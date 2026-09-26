"""Migration graph integrity (hardening W8).

A broken migration graph is invisible to every other test in this suite and to
`alembic upgrade head` on a database that is already at head — and then it
stops a *fresh* deployment from booting at all. That is exactly what happened
during this hardening pass: a new migration reused an existing revision id, and
the failure surfaced only when the API container restarted against a real
Postgres (``Cycle is detected in revisions``).

These tests assert the properties that make the graph deployable, so the
mistake is caught in CI in milliseconds instead of in a running stack:

* every revision id is unique;
* every ``down_revision`` names a revision that exists (no dangling parent);
* there is exactly one head — an ambiguous head means ``upgrade head`` either
  refuses or silently applies only one branch;
* the graph has no cycle, and every revision is reachable from the single
  root, so no migration can be skipped by the ordering.

They read the migration files rather than a database: this is a property of the
source tree, and the point is to test it before any deployment touches it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"

_REVISION = re.compile(r'^revision(?::\s*str)?\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
_DOWN = re.compile(r"^down_revision(?::[^=]+)?=\s*(.+?)\s*$", re.MULTILINE)
_STRING = re.compile(r'["\']([^"\']+)["\']')


def _load_graph() -> dict[str, list[str]]:
    """revision id → its declared parents (usually one)."""
    graph: dict[str, list[str]] = {}
    duplicates: dict[str, list[str]] = {}
    order: dict[str, str] = {}

    for path in sorted(VERSIONS_DIR.glob("*.py")):
        source = path.read_text()
        revision = _REVISION.search(source)
        if revision is None:
            continue
        rev = revision.group(1)
        duplicates.setdefault(rev, []).append(path.name)
        order[rev] = path.name

        down = _DOWN.search(source)
        parents: list[str] = []
        if down is not None:
            raw = down.group(1).strip()
            if raw.lower() not in {"none", "null"}:
                #: Handles a tuple of parents as well as a single string.
                parents = _STRING.findall(raw)
        graph[rev] = parents

    dupes = {rev: names for rev, names in duplicates.items() if len(names) > 1}
    if dupes:
        detail = "; ".join(f"{rev}: {', '.join(names)}" for rev, names in dupes.items())
        pytest.fail(
            "duplicate Alembic revision id(s) — `alembic upgrade head` will fail "
            f"with 'revision present more than once' and the API will not boot: {detail}"
        )
    return graph


def test_every_migration_declares_a_revision() -> None:
    """A file in versions/ without a revision id is silently never applied."""
    declared = set(_load_graph())
    files = {p.name for p in VERSIONS_DIR.glob("*.py")}
    assert declared, "no migrations found — is the versions directory correct?"
    assert len(declared) == len(
        files
    ), f"{len(files)} migration files but {len(declared)} revision ids"


def test_parents_exist() -> None:
    graph = _load_graph()
    known = set(graph)
    dangling = {
        rev: [parent for parent in parents if parent not in known]
        for rev, parents in graph.items()
        if any(parent not in known for parent in parents)
    }
    assert (
        not dangling
    ), f"down_revision points at a revision that does not exist: {dangling}"


def test_exactly_one_head() -> None:
    """Multiple heads make `upgrade head` ambiguous; zero heads means a cycle."""
    graph = _load_graph()
    parents = {parent for parents in graph.values() for parent in parents}
    heads = sorted(set(graph) - parents)
    assert len(heads) == 1, (
        f"expected exactly one head, found {len(heads)}: {heads}. "
        "Two migrations that both extend the same revision create two heads and "
        "`alembic upgrade head` fails."
    )


def test_single_root_and_no_cycle() -> None:
    graph = _load_graph()
    roots = sorted(rev for rev, parents in graph.items() if not parents)
    assert len(roots) == 1, f"expected exactly one root migration, found {roots}"

    #: Walk from the root; every revision must be reachable exactly once.
    children: dict[str, list[str]] = {rev: [] for rev in graph}
    for rev, parents in graph.items():
        for parent in parents:
            children[parent].append(rev)

    seen: list[str] = []
    stack = list(roots)
    while stack:
        rev = stack.pop()
        assert rev not in seen, (
            f"cycle or duplicate path detected at {rev} — Alembic reports this as "
            "'Cycle is detected in revisions' and refuses to migrate"
        )
        seen.append(rev)
        stack.extend(children[rev])

    unreachable = sorted(set(graph) - set(seen))
    assert not unreachable, f"revisions unreachable from the root (they would be silently skipped): {unreachable}"


def test_head_is_the_hardening_head() -> None:
    """The single head is a fact about today; pin it so a rename is deliberate.

    If a new migration lands, update this expectation in the same commit — which
    is the point: the head changes on purpose, never by accident.
    """
    graph = _load_graph()
    parents = {parent for parents in graph.values() for parent in parents}
    heads = sorted(set(graph) - parents)
    assert heads == ["e9f0a1b2c3d4"], (
        f"the migration head changed to {heads}. If that is intended, update this "
        "expectation; if not, a second branch was created by accident."
    )
