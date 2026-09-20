"""ARGUS Trace→Code Mapping (Phase 6 §15–§17).

Connects production telemetry to source code: which definition handled the
request that failed, and which definitions appear in the stack trace it left
behind.

Four signals, ordered by strength, because they are *not* equally trustworthy:

======================  ========  ===============================================
Signal                  Confidence  Why
======================  ========  ===============================================
Stack frame resolved     0.85     The runtime itself printed this file and line.
Route handler matched    0.70     The path is declared in code, but one route may
                                  be served by middleware or a decorator chain.
Service/component name   0.40     A naming convention, not a fact.
Unmapped                 0.00     Stated explicitly, with the reason.
======================  ========  ===============================================

That last row is the point of the module. A mapping that could not be made is
*recorded* with the reason it failed (``no-route-metadata``, ``symbol-not-found``,
``file-not-indexed``), because "we mapped nothing here" and "there was nothing to
map" lead to different next steps for an engineer — and a silently absent row
looks exactly like the former while being indistinguishable from the latter.

§17 is honoured structurally: a stack frame is evidence of *where execution was*,
never of where the defect is. Frames are stored in order and the analysis layer
is free to weight them, but nothing here marks the top frame as the cause.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.code import (
    CodeFile,
    CodeSymbol,
    CodeSymbolType,
    RepositorySnapshot,
    TraceCodeMapping,
    TraceMappingKind,
)
from app.models.incident import Incident
from app.models.observability import LogRecord, SpanRecord, TraceStatus
from app.services.code_query_service import CodeKnowledgeService
from app.services.repository_provider import safe_relative_path, UnsafePathError

logger = logging.getLogger(__name__)

#: Maximum spans examined per incident. A failing trace can have thousands of
#: spans; the ones that matter are the failures and their immediate parents.
MAX_SPANS = 100
MAX_LOGS = 100
MAX_FRAMES_PER_TRACE = 30
MAX_MAPPINGS_PER_RUN = 200

#: Confidence by mapping strategy. Documented in docs/phase-6.md.
CONFIDENCE_STACK_FRAME = 0.85
CONFIDENCE_ROUTE = 0.7
CONFIDENCE_SYMBOL_NAME = 0.55
CONFIDENCE_SERVICE = 0.4

#: Route-ish metadata keys, in the order they are trusted. Different
#: instrumentation libraries spell the same fact differently.
ROUTE_METADATA_KEYS = (
    "http.route",
    "http.target",
    "http.url",
    "url.path",
    "http.path",
    "route",
    "path",
    "endpoint",
    "uri",
)
METHOD_METADATA_KEYS = ("http.method", "http.request.method", "method", "verb")

#: Keys that may hold a stack trace or an exception record.
STACK_METADATA_KEYS = (
    "exception.stacktrace",
    "exception.stack",
    "error.stack",
    "stack",
    "stacktrace",
    "traceback",
    "error.traceback",
)


# ---------------------------------------------------------------------------
# Stack traces
# ---------------------------------------------------------------------------
@dataclass
class StackFrame:
    """One parsed frame. Fields absent in the source format stay ``None``."""

    file_path: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None
    function: Optional[str] = None
    module: Optional[str] = None
    #: Raw text of the frame, kept so a human can see what was actually written.
    raw: str = ""


@dataclass
class StackTrace:
    """A parsed trace, with the exception it was raised for (when stated)."""

    exception_type: Optional[str] = None
    message: Optional[str] = None
    frames: list[StackFrame] = field(default_factory=list)
    format: str = "unknown"

    def resolved_frames(self) -> list[StackFrame]:
        return [frame for frame in self.frames if frame.file_path]


# Python: File "/app/shop/inventory.py", line 42, in check_stock
_PYTHON_FRAME = re.compile(
    r'File\s+"(?P<file>[^"]+)"\s*,\s*line\s*(?P<line>\d+)(?:\s*,\s*in\s*(?P<function>[\w.<>]+))?'
)
# Python compact: File "x.py", line 4  |  inventory.py:42 in check_stock
_PYTHON_COMPACT = re.compile(
    r"(?P<file>[A-Za-z0-9_./\\-]+\.(?:py|pyi))[: ](?:line )?(?P<line>\d+)"
    r"(?:\s*,?\s*in\s+(?P<function>[\w.<>]+))?"
)
# JavaScript/TypeScript: at checkout (/app/src/checkout.ts:42:7)
_JS_FRAME = re.compile(
    r"at\s+(?P<function>[^\s(]+)\s+\((?P<file>[^():]+):(?P<line>\d+):(?P<column>\d+)\)"
)
# JavaScript anonymous: at /app/src/checkout.ts:42:7
_JS_ANON = re.compile(r"at\s+(?P<file>[^():]+):(?P<line>\d+):(?P<column>\d+)")
# Java: at com.foo.Bar.baz(Bar.java:42)
_JAVA_FRAME = re.compile(
    r"at\s+(?P<function>[\w.$]+)\((?P<file>[\w.$]+\.java):(?P<line>\d+)\)"
)
# Go: path/to/file.go:42 +0x1a
_GO_FRAME = re.compile(r"(?P<file>[\w./\\-]+\.go):(?P<line>\d+)(?:\s+\+0x[0-9a-f]+)?")
# Java/Go/Ruby head line: "ValueError: message" / "java.lang.NullPointerException: x"
_EXCEPTION_HEAD = re.compile(
    r"^(?P<type>(?:[A-Za-z_][\w.]*\.)*[A-Z]\w*(?:Error|Exception|Throwable|Fault))\s*:?\s*(?P<message>.*)$"
)


class StackTraceAnalyzer:
    """Parses stack traces out of log text and span metadata (§16)."""

    #: Recognised formats, tried in order: the more specific patterns first so a
    #: Python frame is never parsed as a JavaScript one.
    FORMATS = (
        ("python", _PYTHON_FRAME),
        ("python", _PYTHON_COMPACT),
        ("javascript", _JS_FRAME),
        ("javascript", _JS_ANON),
        ("java", _JAVA_FRAME),
        ("go", _GO_FRAME),
    )

    def parse(self, text: Optional[str]) -> Optional[StackTrace]:
        """Parse ``text``; returns ``None`` when nothing trace-shaped is present."""
        if not text or len(text) < 10:
            return None
        trace = StackTrace()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if trace.exception_type is None:
                head = _EXCEPTION_HEAD.match(stripped)
                if head:
                    trace.exception_type = head.group("type")
                    trace.message = (head.group("message") or "").strip()[:500]
                    continue
            frame = self._parse_line(stripped, trace)
            if frame is not None:
                if len(trace.frames) < MAX_FRAMES_PER_TRACE:
                    trace.frames.append(frame)
                continue
        if not trace.frames and trace.exception_type is None:
            return None
        trace.format = self._dominant_format(trace.frames)
        return trace

    def _parse_line(self, line: str, trace: StackTrace) -> Optional[StackFrame]:
        for name, pattern in self.FORMATS:
            match = pattern.search(line)
            if not match:
                continue
            groups = match.groupdict()
            file_path = groups.get("file")
            if not file_path:
                continue
            try:
                line_number = int(groups["line"]) if groups.get("line") else None
            except (TypeError, ValueError):
                line_number = None
            if line_number is None or line_number <= 0:
                continue
            try:
                column = int(groups["column"]) if groups.get("column") else None
            except (TypeError, ValueError):
                column = None
            function = groups.get("function")
            module = None
            if name == "java" and function and "." in function:
                module, _, function = function.rpartition(".")
            trace.format = name if trace.format == "unknown" else trace.format
            return StackFrame(
                file_path=file_path,
                line=line_number,
                column=column,
                function=function,
                module=module,
                raw=line[:500],
            )
        return None

    def _dominant_format(self, frames: Iterable[StackFrame]) -> str:
        for name, pattern in self.FORMATS:
            for frame in frames:
                if pattern.search(frame.raw):
                    return name
        return "unknown"


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------
class TraceCodeMapper:
    """Maps an incident's failing spans and stack frames onto code."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.analyzer = StackTraceAnalyzer()
        self.knowledge = CodeKnowledgeService(session)

    async def map_incident(
        self,
        incident: Incident,
        snapshot: Optional[RepositorySnapshot],
        *,
        replace: bool = True,
    ) -> list[TraceCodeMapping]:
        """Create the incident's trace→code mappings, returning the rows.

        Mappings are per incident and per snapshot, because the same span maps to
        different lines once the code moves. Re-mapping an incident against a new
        snapshot replaces the old rows rather than mixing revisions.
        """
        if snapshot is None:
            logger.info("no snapshot for incident %s; cannot map traces", incident.id)
            return []

        spans = await self._failing_spans(incident)
        logs = await self._error_logs(incident)
        if replace:
            await self._clear(incident, spans)
        snapshot_files = await self._snapshot_paths(snapshot.id)

        rows: list[TraceCodeMapping] = []
        stacks: dict[str, StackTrace] = {}
        for log in logs:
            text = self._log_text(log)
            parsed = self.analyzer.parse(text)
            if parsed is not None and parsed.resolved_frames():
                #: Kept per trace when the log carries one, so a frame can be
                #: attributed to the span it belongs to instead of to the incident.
                key = log.trace_id or f"log:{log.id}"
                stacks.setdefault(key, parsed)

        for span in spans:
            if len(rows) >= MAX_MAPPINGS_PER_RUN:
                break
            rows.extend(await self._map_span(span, snapshot, snapshot_files, stacks))
        for key, stack in stacks.items():
            if len(rows) >= MAX_MAPPINGS_PER_RUN:
                break
            rows.extend(
                await self._map_stack(
                    stack,
                    snapshot,
                    snapshot_files,
                    trace_id=key if not key.startswith("log:") else None,
                    span_id=None,
                    component_id=None,
                )
            )

        if rows:
            self.session.add_all(rows)
            await self.session.flush()
        return rows

    # -- sources -----------------------------------------------------------
    async def _failing_spans(self, incident: Incident) -> list[SpanRecord]:
        stmt = (
            select(SpanRecord)
            .where(
                SpanRecord.project_id == incident.project_id,
                SpanRecord.status == TraceStatus.ERROR,
            )
            .order_by(SpanRecord.start_time.desc())
            .limit(MAX_SPANS)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def _error_logs(self, incident: Incident) -> list[LogRecord]:
        stmt = (
            select(LogRecord)
            .where(LogRecord.project_id == incident.project_id)
            .order_by(LogRecord.timestamp.desc())
            .limit(MAX_LOGS)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def _snapshot_paths(self, snapshot_id) -> set[str]:
        rows = (
            await self.session.execute(
                select(CodeFile.path).where(CodeFile.snapshot_id == snapshot_id)
            )
        ).all()
        return {row[0] for row in rows}

    async def _clear(self, incident: Incident, spans: list[SpanRecord]) -> None:
        """Remove this incident's previous mappings, and only this incident's.

        Scoped to the spans being re-mapped rather than to the whole project: a
        project-wide delete would silently erase a concurrent investigation's
        mappings, and the table has no incident column to scope by directly.
        """
        from sqlalchemy import delete, or_

        span_ids = [span.span_id for span in spans if span.span_id]
        trace_ids = [span.trace_id for span in spans if span.trace_id]
        if not span_ids and not trace_ids:
            return
        conditions = []
        if span_ids:
            conditions.append(TraceCodeMapping.span_id.in_(span_ids))
        if trace_ids:
            conditions.append(TraceCodeMapping.trace_id.in_(trace_ids))
        await self.session.execute(
            delete(TraceCodeMapping).where(
                TraceCodeMapping.project_id == incident.project_id,
                or_(*conditions),
            )
        )
        await self.session.flush()

    # -- mapping strategies ------------------------------------------------
    async def _map_span(
        self,
        span: SpanRecord,
        snapshot: RepositorySnapshot,
        snapshot_files: set[str],
        stacks: dict[str, StackTrace],
    ) -> list[TraceCodeMapping]:
        metadata = span.metadata_ or {}
        route, method = self._route_of(span, metadata)
        service = self._service_of(span, metadata)
        rows: list[TraceCodeMapping] = []

        if route:
            handlers = await self.knowledge.find_route_handler(
                snapshot.id, path=route, method=method, limit=5
            )
            if handlers:
                for handler in handlers[:3]:
                    rows.append(
                        self._row(
                            span,
                            snapshot,
                            mapping_kind=TraceMappingKind.ROUTE,
                            symbol=handler,
                            endpoint=route,
                            http_method=method,
                            confidence=CONFIDENCE_ROUTE,
                            evidence=(
                                f"route {method or 'ANY'} {route} is declared by "
                                f"{handler.qualified_name}"
                            ),
                        )
                    )
                return rows

        stack = stacks.get(span.trace_id or "")
        if stack is not None:
            stack_rows = await self._map_stack(
                stack,
                snapshot,
                snapshot_files,
                trace_id=span.trace_id,
                span_id=span.span_id,
                component_id=span.component_id,
            )
            if stack_rows:
                rows.extend(stack_rows)

        if rows:
            return rows

        if service:
            symbols = await self._symbols_for_service(snapshot.id, service)
            if symbols:
                return [
                    self._row(
                        span,
                        snapshot,
                        mapping_kind=TraceMappingKind.SERVICE,
                        symbol=symbol,
                        endpoint=route,
                        http_method=method,
                        confidence=CONFIDENCE_SERVICE,
                        evidence=(
                            f"service name {service!r} matches the file path of "
                            f"{symbol.qualified_name}"
                        ),
                    )
                    for symbol in symbols[:2]
                ]

        #: Nothing matched, and the reason is recorded. ``no-route-metadata`` and
        #: ``symbol-not-found`` are different problems: the first needs better
        #: instrumentation, the second needs a wider index or a different
        #: snapshot — and an engineer should be told which.
        reason = self._unmapped_reason(route, stack, service, snapshot_files)
        return [
            self._row(
                span,
                snapshot,
                mapping_kind=TraceMappingKind.UNMAPPED,
                symbol=None,
                endpoint=route,
                http_method=method,
                confidence=0.0,
                evidence="no code mapping could be established from the stored telemetry",
                unmapped_reason=reason,
            )
        ]

    async def _map_stack(
        self,
        stack: StackTrace,
        snapshot: RepositorySnapshot,
        snapshot_files: set[str],
        *,
        trace_id: Optional[str],
        span_id: Optional[str],
        component_id,
    ) -> list[TraceCodeMapping]:
        """Resolve frames to symbols, innermost first (§17).

        Frames whose file is not in the snapshot are counted, not silently
        dropped: ``file-not-indexed`` is how you discover that the deployed
        revision and the indexed revision disagree.
        """
        rows: list[TraceCodeMapping] = []
        for frame in stack.resolved_frames():
            if len(rows) >= 5:
                break
            relative = _match_snapshot_path(snapshot_files, frame.file_path or "")
            if relative is None:
                rows.append(
                    self._stack_row(
                        stack,
                        snapshot,
                        frame,
                        trace_id,
                        span_id,
                        component_id,
                        symbol=None,
                        relative=None,
                        unmapped_reason="file-not-indexed",
                    )
                )
                continue
            symbol = await self._symbol_at_line(snapshot.id, relative, frame.line or 0)
            if symbol is None and frame.function:
                found = await self.knowledge.find_symbol(
                    snapshot.id, frame.function, limit=3
                )
                symbol = found[0] if found else None
            rows.append(
                self._stack_row(
                    stack,
                    snapshot,
                    frame,
                    trace_id,
                    span_id,
                    component_id,
                    symbol=symbol,
                    relative=relative,
                    unmapped_reason=None if symbol else "symbol-not-found",
                )
            )
        return rows

    async def _symbol_at_line(
        self, snapshot_id, file_path: str, line: int
    ) -> Optional[CodeSymbol]:
        """The innermost definition containing ``line``.

        Innermost, not first: an exception raised inside a nested helper belongs
        to the helper, and picking the enclosing class instead would name the
        wrong function while looking entirely plausible.
        """
        if line <= 0:
            return None
        stmt = (
            select(CodeSymbol)
            .where(
                CodeSymbol.snapshot_id == snapshot_id,
                CodeSymbol.file_path == file_path,
                CodeSymbol.start_line <= line,
                CodeSymbol.end_line >= line,
            )
            .limit(10)
        )
        candidates = list((await self.session.execute(stmt)).scalars().all())
        if not candidates:
            return None
        return min(candidates, key=lambda symbol: (symbol.end_line - symbol.start_line))

    async def _symbols_for_service(self, snapshot_id, service: str) -> list[CodeSymbol]:
        needle = (service or "").strip().strip("/")
        if not needle:
            return []
        stem = needle.split("/")[-1].split(".")[0]
        if not stem:
            return []
        #: Only entry-point definitions are offered: a service name matching an
        #: internal helper's file would send an engineer to the wrong layer, and a
        #: weak match that looks plausible is worse than no match.
        stmt = (
            select(CodeSymbol)
            .where(
                CodeSymbol.snapshot_id == snapshot_id,
                CodeSymbol.symbol_type.in_(
                    [CodeSymbolType.ROUTE, CodeSymbolType.HANDLER, CodeSymbolType.CLASS]
                ),
                CodeSymbol.file_path.ilike(f"%{stem}%"),
            )
            .order_by(CodeSymbol.start_line)
            .limit(2)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # -- extraction --------------------------------------------------------
    def _route_of(
        self, span: SpanRecord, metadata: dict
    ) -> tuple[Optional[str], Optional[str]]:
        route = None
        for key in ROUTE_METADATA_KEYS:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                route = value.strip()
                break
        if route is None:
            #: ``checkout./checkout`` (Phase 5's runner) and ``GET /orders``
            #: both carry the path as the operation's tail.
            operation = span.operation or ""
            for token in operation.split():
                if token.startswith("/"):
                    route = token
                    break
            if route is None and "/" in operation:
                tail = operation.split("/", 1)[1]
                route = "/" + tail.lstrip("/")
        method = None
        for key in METHOD_METADATA_KEYS:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                method = value.strip().upper()
                break
        if route:
            route = route.split("?", 1)[0]
            if not route.startswith("/"):
                route = "/" + route
        return route, method

    def _service_of(self, span: SpanRecord, metadata: dict) -> Optional[str]:
        for key in ("service.name", "service", "component", "app", "service_name"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        operation = span.operation or ""
        if "." in operation:
            head = operation.split(".", 1)[0].strip()
            if head and "/" not in head:
                return head
        return None

    def _log_text(self, log: LogRecord) -> str:
        parts = [log.message or ""]
        for payload in (log.metadata_, log.raw_payload):
            if isinstance(payload, dict):
                for key in STACK_METADATA_KEYS:
                    value = payload.get(key)
                    if isinstance(value, str) and value:
                        parts.append(value)
        return "\n".join(parts)

    def _unmapped_reason(
        self,
        route: Optional[str],
        stack: Optional[StackTrace],
        service: Optional[str],
        snapshot_files: set[str],
    ) -> str:
        if stack is None:
            #: Either the telemetry never carried a trace, or it carried one whose
            #: frames all came from outside the application (a framework, a
            #: driver). The distinction is the whole value of this field.
            return (
                "no-route-metadata"
                if route is None and service is None
                else "no-stack-trace"
            )
        frames = stack.resolved_frames()
        if not frames:
            return "no-usable-frames"
        if not any(
            _match_snapshot_path(snapshot_files, frame.file_path or "")
            for frame in frames
        ):
            return "file-not-indexed"
        if route:
            return "route-symbol-not-found"
        return "symbol-not-found"

    # -- row builders ------------------------------------------------------
    def _row(
        self,
        span: SpanRecord,
        snapshot: RepositorySnapshot,
        *,
        mapping_kind: TraceMappingKind,
        symbol: Optional[CodeSymbol],
        endpoint: Optional[str],
        http_method: Optional[str],
        confidence: float,
        evidence: str,
        unmapped_reason: Optional[str] = None,
    ) -> TraceCodeMapping:
        return TraceCodeMapping(
            project_id=span.project_id,
            snapshot_id=snapshot.id,
            component_id=span.component_id,
            trace_id=span.trace_id,
            span_id=span.span_id,
            operation=(span.operation or None),
            service_name=self._service_of(span, span.metadata_ or {}),
            endpoint=endpoint,
            http_method=http_method,
            mapping_kind=mapping_kind,
            symbol_id=symbol.id if symbol else None,
            file_path=symbol.file_path if symbol else None,
            start_line=symbol.start_line if symbol else None,
            end_line=symbol.end_line if symbol else None,
            confidence=confidence,
            evidence=evidence,
            unmapped_reason=unmapped_reason,
            mapping_metadata={
                "span_status": span.status.value if span.status else None,
                "duration_ms": span.duration_ms,
                "mapped_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _stack_row(
        self,
        stack: StackTrace,
        snapshot: RepositorySnapshot,
        frame: StackFrame,
        trace_id: Optional[str],
        span_id: Optional[str],
        component_id,
        *,
        symbol: Optional[CodeSymbol],
        relative: Optional[str],
        unmapped_reason: Optional[str],
    ) -> TraceCodeMapping:
        return TraceCodeMapping(
            project_id=snapshot.project_id,
            snapshot_id=snapshot.id,
            component_id=component_id,
            trace_id=trace_id,
            span_id=span_id,
            operation=frame.function,
            service_name=None,
            endpoint=None,
            http_method=None,
            mapping_kind=(
                TraceMappingKind.STACK_FRAME if symbol else TraceMappingKind.UNMAPPED
            ),
            symbol_id=symbol.id if symbol else None,
            file_path=(symbol.file_path if symbol else relative)
            or (frame.file_path or "")[:1024],
            start_line=symbol.start_line if symbol else frame.line,
            end_line=symbol.end_line if symbol else frame.line,
            confidence=CONFIDENCE_STACK_FRAME if symbol else 0.0,
            evidence=(
                f"stack frame {frame.raw[:200]}"
                + (f" resolved to {symbol.qualified_name}" if symbol else "")
            ),
            unmapped_reason=unmapped_reason,
            mapping_metadata={
                "stack_format": stack.format,
                "exception_type": stack.exception_type,
                "exception_message": stack.message,
                "frame_function": frame.function,
                "column": frame.column,
            },
        )


def match_frame_to_snapshot(snapshot_files: set[str], frame_file: str) -> Optional[str]:
    """Public wrapper over the suffix-matching used by the mapper.

    The debug context builder needs the same frame→snapshot translation the
    mapper uses, and duplicating it would let the two disagree: a frame the
    mapper resolved to ``shop/checkout.py`` but the builder left as
    ``/srv/app/shop/checkout.py`` produces a claim the validator must reject even
    though the location is real.
    """
    return _match_snapshot_path(snapshot_files, frame_file)


def _match_snapshot_path(snapshot_files: set[str], frame_file: str) -> Optional[str]:
    """Match a runtime file path to a path inside the snapshot.

    Stack traces carry whatever path the process saw — an absolute container path
    like ``/app/shop/inventory.py`` or a host path from the developer's machine.
    Matching is therefore by suffix, longest candidate first, so the most specific
    file wins. A path that cannot be confined to a repository-relative form is
    rejected outright rather than guessed at.
    """
    if not frame_file:
        return None
    normalised = frame_file.replace("\\", "/").strip()
    if normalised in snapshot_files:
        return normalised
    if normalised.startswith("/"):
        #: Try every suffix of the absolute path: the snapshot may itself contain
        #: the path prefix (an application that checks out into its own tree).
        parts = [
            segment for segment in normalised.split("/") if segment not in ("", ".")
        ]
        for start in range(len(parts)):
            candidate = "/".join(parts[start:])
            try:
                safe_relative_path(candidate)
            except UnsafePathError:
                continue
            if candidate in snapshot_files:
                return candidate
    candidates = [
        path
        for path in snapshot_files
        if normalised.endswith("/" + path) or path.endswith("/" + normalised)
    ]
    if candidates:
        return max(candidates, key=len)
    try:
        relative = safe_relative_path(normalised.lstrip("./"))
    except UnsafePathError:
        return None
    return relative if relative in snapshot_files else None
