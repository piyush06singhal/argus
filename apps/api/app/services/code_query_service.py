"""ARGUS Code Knowledge & Search (Phase 6 §14, §23).

The read-only query surface over indexed code: symbols, references, callers,
callees, routes, database calls, text search.

Every method is **bounded**, and every bound is a parameter with a default rather
than an internal constant. Two reasons, both practical:

* an unbounded query on a 20,000-file snapshot is a denial-of-service against the
  same database the API is answering from;
* the AI debugger's tool budget (§39) is expressed in *results*, so the service
  has to be able to honour a limit it was given rather than one it chose.

Nothing here resolves or infers. The graph was resolved at indexing time and
stored with the strategy that resolved it; these queries read that decision back
instead of making a fresh one, so a caller can never get a different answer than
the analysis was based on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.code import (
    CodeFile,
    CodeReference,
    CodeRelationship,
    CodeRelationshipType,
    CodeRiskSignal,
    CodeSymbol,
    CodeSymbolType,
    ReferenceKind,
    RepositorySnapshot,
    RiskSignalType,
)

logger = logging.getLogger(__name__)

#: Default page sizes. Deliberately small: the debugger is answering a question,
#: not exporting a repository.
DEFAULT_SYMBOL_LIMIT = 50
MAX_SYMBOL_LIMIT = 500
DEFAULT_REFERENCE_LIMIT = 50
MAX_REFERENCE_LIMIT = 500
DEFAULT_EDGE_LIMIT = 50
MAX_EDGE_LIMIT = 500
DEFAULT_SEARCH_LIMIT = 25
MAX_SEARCH_LIMIT = 200

#: Symbol types worth offering as a *call* target / route handler.
CALLABLE_TYPES = (
    CodeSymbolType.FUNCTION,
    CodeSymbolType.METHOD,
    CodeSymbolType.CLASS,
    CodeSymbolType.ROUTE,
    CodeSymbolType.HANDLER,
)


def clamp(value: Optional[int], default: int, maximum: int) -> int:
    if value is None:
        return default
    return max(1, min(int(value), maximum))


@dataclass
class SymbolHit:
    """A symbol row plus the context the debugger always needs with it."""

    symbol: CodeSymbol
    #: Signals recorded for this symbol at indexing time (§44).
    signals: list[CodeRiskSignal] = field(default_factory=list)
    #: Resolved callers/callees counts, so the UI can show them without N queries.
    caller_count: int = 0
    callee_count: int = 0

    def reference(self) -> str:
        """Canonical evidence reference (§29)."""
        return f"FILE:{self.symbol.file_path}:{self.symbol.start_line}-{self.symbol.end_line}"

    def as_dict(self) -> dict:
        return {
            "id": str(self.symbol.id),
            "file_path": self.symbol.file_path,
            "symbol_name": self.symbol.symbol_name,
            "qualified_name": self.symbol.qualified_name,
            "symbol_type": self.symbol.symbol_type.value,
            "language": self.symbol.language,
            "start_line": self.symbol.start_line,
            "end_line": self.symbol.end_line,
            "signature": self.symbol.signature,
            "route": self.symbol.route,
            "http_method": self.symbol.http_method,
            "complexity": self.symbol.complexity,
            "is_async": self.symbol.is_async,
            "reference": self.reference(),
            "signals": [
                {
                    "type": signal.signal_type.value,
                    "value": signal.value,
                    "unit": signal.unit,
                    "detail": signal.detail,
                }
                for signal in self.signals
            ],
            "caller_count": self.caller_count,
            "callee_count": self.callee_count,
        }


class CodeKnowledgeService:
    """Bounded, read-only queries over one snapshot's code intelligence."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Symbols
    # ------------------------------------------------------------------
    async def find_symbol(
        self,
        snapshot_id,
        name: str,
        *,
        limit: Optional[int] = None,
        symbol_type: Optional[CodeSymbolType] = None,
        file_path: Optional[str] = None,
    ) -> list[CodeSymbol]:
        """Find definitions by name, qualified name or file path.

        Matching is exact-first and case-insensitive-second, in that order, so an
        exact hit can never be buried under a fuzzy one.
        """
        bound = clamp(limit, DEFAULT_SYMBOL_LIMIT, MAX_SYMBOL_LIMIT)
        needle = (name or "").strip()
        if not needle:
            return []
        stmt = select(CodeSymbol).where(CodeSymbol.snapshot_id == snapshot_id)
        if symbol_type is not None:
            stmt = stmt.where(CodeSymbol.symbol_type == symbol_type)
        if file_path:
            stmt = stmt.where(CodeSymbol.file_path == file_path)
        exact = or_(
            CodeSymbol.symbol_name == needle,
            CodeSymbol.qualified_name == needle,
        )
        prefix = CodeSymbol.qualified_name.ilike(f"%{needle}%")
        stmt = stmt.where(or_(exact, prefix))
        #: ``case`` ordering puts exact matches first without a second query.
        ordering = func.coalesce(
            func.nullif(CodeSymbol.qualified_name, needle),  # exact → NULL → first
            CodeSymbol.qualified_name,
        )
        stmt = stmt.order_by(ordering, CodeSymbol.start_line).limit(bound)
        return list((await self.session.execute(stmt)).scalars().all())

    async def symbol_by_id(self, symbol_id) -> Optional[CodeSymbol]:
        return (
            (
                await self.session.execute(
                    select(CodeSymbol).where(CodeSymbol.id == symbol_id)
                )
            )
            .scalars()
            .first()
        )

    async def symbols_in_file(
        self, snapshot_id, file_path: str, *, limit: Optional[int] = None
    ) -> list[CodeSymbol]:
        bound = clamp(limit, DEFAULT_SYMBOL_LIMIT, MAX_SYMBOL_LIMIT)
        stmt = (
            select(CodeSymbol)
            .where(
                CodeSymbol.snapshot_id == snapshot_id,
                CodeSymbol.file_path == file_path,
            )
            .order_by(CodeSymbol.start_line)
            .limit(bound)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def symbol_hits(
        self, snapshot_id, symbols: list[CodeSymbol]
    ) -> list[SymbolHit]:
        """Decorate symbols with their signals and resolved edge counts."""
        if not symbols:
            return []
        ids = [symbol.id for symbol in symbols]
        signal_rows = (
            (
                await self.session.execute(
                    select(CodeRiskSignal).where(CodeRiskSignal.symbol_id.in_(ids))
                )
            )
            .scalars()
            .all()
        )
        by_symbol: dict[object, list[CodeRiskSignal]] = {}
        for signal in signal_rows:
            by_symbol.setdefault(signal.symbol_id, []).append(signal)

        callees = await self._edge_counts(
            CodeRelationship.source_symbol_id, CodeRelationship.target_symbol_id, ids
        )
        callers = await self._edge_counts(
            CodeRelationship.target_symbol_id, CodeRelationship.source_symbol_id, ids
        )
        return [
            SymbolHit(
                symbol=symbol,
                signals=by_symbol.get(symbol.id, []),
                caller_count=callers.get(symbol.id, 0),
                callee_count=callees.get(symbol.id, 0),
            )
            for symbol in symbols
        ]

    async def _edge_counts(self, group_column, filter_column, ids: list) -> dict:
        rows = (
            await self.session.execute(
                select(group_column, func.count())
                .where(filter_column.in_(ids))
                .group_by(group_column)
            )
        ).all()
        return {row[0]: row[1] for row in rows}

    # ------------------------------------------------------------------
    # References, callers, callees
    # ------------------------------------------------------------------
    async def find_references(
        self,
        snapshot_id,
        name: str,
        *,
        limit: Optional[int] = None,
        resolved_only: bool = False,
        kinds: Optional[tuple[ReferenceKind, ...]] = None,
    ) -> list[CodeReference]:
        """Occurrences of a name, newest-first not required (line order reads better)."""
        bound = clamp(limit, DEFAULT_REFERENCE_LIMIT, MAX_REFERENCE_LIMIT)
        needle = (name or "").strip()
        if not needle:
            return []
        stmt = select(CodeReference).where(
            CodeReference.snapshot_id == snapshot_id,
            CodeReference.name == needle,
        )
        if resolved_only:
            stmt = stmt.where(CodeReference.symbol_id.is_not(None))
        if kinds:
            stmt = stmt.where(CodeReference.reference_kind.in_(list(kinds)))
        stmt = stmt.order_by(CodeReference.file_path, CodeReference.line).limit(bound)
        return list((await self.session.execute(stmt)).scalars().all())

    async def find_callers(
        self,
        symbol_id,
        *,
        limit: Optional[int] = None,
        relationship_types: Optional[tuple[CodeRelationshipType, ...]] = None,
    ) -> list[tuple[CodeRelationship, CodeSymbol]]:
        """Definitions that call into ``symbol_id``, with the edge that says so."""
        bound = clamp(limit, DEFAULT_EDGE_LIMIT, MAX_EDGE_LIMIT)
        stmt = (
            select(CodeRelationship, CodeSymbol)
            .join(CodeSymbol, CodeSymbol.id == CodeRelationship.source_symbol_id)
            .where(CodeRelationship.target_symbol_id == symbol_id)
        )
        if relationship_types:
            stmt = stmt.where(
                CodeRelationship.relationship_type.in_(list(relationship_types))
            )
        stmt = stmt.order_by(CodeRelationship.confidence.desc()).limit(bound)
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def find_callees(
        self,
        symbol_id,
        *,
        limit: Optional[int] = None,
        relationship_types: Optional[tuple[CodeRelationshipType, ...]] = None,
    ) -> list[tuple[CodeRelationship, CodeSymbol]]:
        bound = clamp(limit, DEFAULT_EDGE_LIMIT, MAX_EDGE_LIMIT)
        stmt = (
            select(CodeRelationship, CodeSymbol)
            .join(CodeSymbol, CodeSymbol.id == CodeRelationship.target_symbol_id)
            .where(CodeRelationship.source_symbol_id == symbol_id)
        )
        if relationship_types:
            stmt = stmt.where(
                CodeRelationship.relationship_type.in_(list(relationship_types))
            )
        stmt = stmt.order_by(CodeRelationship.confidence.desc()).limit(bound)
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def find_related_files(
        self, symbol_id, *, limit: Optional[int] = None
    ) -> list[str]:
        """Files reachable from a symbol in one hop, either direction."""
        bound = clamp(limit, 20, 200)
        outgoing = (
            await self.session.execute(
                select(CodeSymbol.file_path)
                .join(
                    CodeRelationship, CodeRelationship.target_symbol_id == CodeSymbol.id
                )
                .where(CodeRelationship.source_symbol_id == symbol_id)
                .distinct()
                .limit(bound)
            )
        ).all()
        incoming = (
            await self.session.execute(
                select(CodeSymbol.file_path)
                .join(
                    CodeRelationship, CodeRelationship.source_symbol_id == CodeSymbol.id
                )
                .where(CodeRelationship.target_symbol_id == symbol_id)
                .distinct()
                .limit(bound)
            )
        ).all()
        seen: list[str] = []
        for row in list(outgoing) + list(incoming):
            if row[0] not in seen:
                seen.append(row[0])
        return seen[:bound]

    # ------------------------------------------------------------------
    # Routes and data access
    # ------------------------------------------------------------------
    async def find_route_handler(
        self,
        snapshot_id,
        *,
        path: Optional[str] = None,
        method: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[CodeSymbol]:
        """Find the definition serving a route (§15).

        Two match strengths, tried in order: the exact declared route, then the
        path portion regardless of HTTP method. A path-only match is returned
        *only* when no method was asked for or no method-specific match exists,
        because ``GET /orders`` and ``POST /orders`` are different handlers.
        """
        bound = clamp(limit, DEFAULT_SYMBOL_LIMIT, MAX_SYMBOL_LIMIT)
        normalised = (path or "").strip()
        if not normalised:
            return []
        if not normalised.startswith("/"):
            normalised = "/" + normalised
        route_columns = (CodeSymbol.route,)
        base = select(CodeSymbol).where(
            CodeSymbol.snapshot_id == snapshot_id,
            *[column.is_not(None) for column in route_columns],
        )
        if method:
            exact = base.where(
                CodeSymbol.route == f"{method.upper()} {normalised}"
            ).limit(bound)
            rows = list((await self.session.execute(exact)).scalars().all())
            if rows:
                return rows
        #: Trailing-slash and prefix variants of the same path.
        candidates = (
            base.where(CodeSymbol.route.ilike(f"%{normalised}%"))
            .order_by(CodeSymbol.start_line)
            .limit(bound)
        )
        return list((await self.session.execute(candidates)).scalars().all())

    async def find_database_calls(
        self, snapshot_id, *, limit: Optional[int] = None
    ) -> list[CodeSymbol]:
        """Definitions that talk to a data store, from stored signals."""
        bound = clamp(limit, DEFAULT_SYMBOL_LIMIT, MAX_SYMBOL_LIMIT)
        stmt = (
            select(CodeSymbol)
            .join(CodeRiskSignal, CodeRiskSignal.symbol_id == CodeSymbol.id)
            .where(
                CodeRiskSignal.snapshot_id == snapshot_id,
                CodeRiskSignal.signal_type.in_(
                    [
                        RiskSignalType.DATABASE_OPERATION,
                        RiskSignalType.EXTERNAL_API_CALL,
                    ]
                ),
            )
            .order_by(CodeRiskSignal.value.desc())
            .limit(bound)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # ------------------------------------------------------------------
    # Text search
    # ------------------------------------------------------------------
    async def search_code(
        self,
        snapshot_id,
        query: str,
        *,
        limit: Optional[int] = None,
        include_references: bool = True,
    ) -> dict:
        """Symbol-aware text search (§23).

        Returns symbol-name matches and, separately, *source-text* matches drawn
        from stored definitions. The two are kept apart because "a definition is
        called ``timeout``" and "the string ``timeout`` appears in this function"
        are different findings, and merging them makes the first invisible.
        """
        bound = clamp(limit, DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT)
        needle = (query or "").strip()
        if not needle:
            return {"query": query, "symbols": [], "sources": [], "references": []}
        pattern = f"%{needle}%"

        symbol_rows = (
            (
                await self.session.execute(
                    select(CodeSymbol)
                    .where(
                        CodeSymbol.snapshot_id == snapshot_id,
                        or_(
                            CodeSymbol.symbol_name.ilike(pattern),
                            CodeSymbol.qualified_name.ilike(pattern),
                        ),
                    )
                    .order_by(CodeSymbol.start_line)
                    .limit(bound)
                )
            )
            .scalars()
            .all()
        )

        source_rows = (
            (
                await self.session.execute(
                    select(CodeSymbol)
                    .where(
                        CodeSymbol.snapshot_id == snapshot_id,
                        CodeSymbol.source.ilike(pattern),
                    )
                    .order_by(CodeSymbol.start_line)
                    .limit(bound)
                )
            )
            .scalars()
            .all()
        )

        references: list[CodeReference] = []
        if include_references:
            references = list(
                (
                    await self.session.execute(
                        select(CodeReference)
                        .where(
                            CodeReference.snapshot_id == snapshot_id,
                            CodeReference.name.ilike(pattern),
                        )
                        .order_by(CodeReference.file_path, CodeReference.line)
                        .limit(bound)
                    )
                )
                .scalars()
                .all()
            )

        return {
            "query": query,
            "symbols": symbol_rows,
            "sources": [row for row in source_rows if row not in symbol_rows],
            "references": references,
        }

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------
    async def snapshot_summary(self, snapshot_id) -> dict:
        snapshot = (
            (
                await self.session.execute(
                    select(RepositorySnapshot).where(
                        RepositorySnapshot.id == snapshot_id
                    )
                )
            )
            .scalars()
            .first()
        )
        if snapshot is None:
            return {}
        #: Counts are queried rather than read off the snapshot row: the row's
        #: totals are written at the end of an index run, so a partially failed
        #: run would report a number that does not describe the stored rows.
        files = (
            await self.session.execute(
                select(func.count(CodeFile.id)).where(CodeFile.snapshot_id == snapshot_id)
            )
        ).scalar_one()
        symbols = (
            await self.session.execute(
                select(func.count(CodeSymbol.id)).where(
                    CodeSymbol.snapshot_id == snapshot_id
                )
            )
        ).scalar_one()
        relationships = (
            await self.session.execute(
                select(func.count(CodeRelationship.id)).where(
                    CodeRelationship.snapshot_id == snapshot_id
                )
            )
        ).scalar_one()
        references = (
            await self.session.execute(
                select(func.count(CodeReference.id)).where(
                    CodeReference.snapshot_id == snapshot_id
                )
            )
        ).scalar_one()
        tests = (
            await self.session.execute(
                select(func.count(CodeFile.id)).where(
                    CodeFile.snapshot_id == snapshot_id, CodeFile.is_test.is_(True)
                )
            )
        ).scalar_one()
        language_rows = (
            await self.session.execute(
                select(CodeFile.language, func.count(CodeFile.id))
                .where(CodeFile.snapshot_id == snapshot_id)
                .group_by(CodeFile.language)
            )
        ).all()
        signal_rows = (
            await self.session.execute(
                select(CodeRiskSignal.signal_type, func.count(CodeRiskSignal.id))
                .where(CodeRiskSignal.snapshot_id == snapshot_id)
                .group_by(CodeRiskSignal.signal_type)
            )
        ).all()
        return {
            "id": str(snapshot.id),
            "commit_sha": snapshot.commit_sha,
            "branch": snapshot.branch,
            "status": snapshot.status.value,
            "version_status": snapshot.version_status.value,
            "version_evidence": snapshot.version_evidence,
            "indexed_at": snapshot.indexed_at.isoformat()
            if snapshot.indexed_at
            else None,
            "file_count": snapshot.file_count,
            "symbol_count": snapshot.symbol_count,
            "languages": snapshot.languages,
            "index_metadata": snapshot.index_metadata,
            "files": int(files or 0),
            "symbols": int(symbols or 0),
            "relationships": int(relationships or 0),
            "references": int(references or 0),
            "tests": int(tests or 0),
            "languages_by_count": {
                (row[0] or "unknown"): int(row[1]) for row in language_rows
            },
            "signals_by_type": {
                (row[0].value if hasattr(row[0], "value") else str(row[0])): int(row[1])
                for row in signal_rows
            },
        }
