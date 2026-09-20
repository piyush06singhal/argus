"""ARGUS Code Parsers (Phase 6 §10–§12).

Turns source text into **definitions** (:class:`ParsedSymbol`) and **name
occurrences** (:class:`ParsedReference`). Nothing here resolves anything to
another symbol or decides what is suspicious: a parser reports what is in the
file, and nothing else.

Two parsers, and the difference between them is stated rather than hidden:

* :class:`PythonParser` uses the **real CPython AST** (``ast.parse``). Its output
  is exact: a function's line range is the span the compiler would report.
* :class:`JavaScriptParser` handles JavaScript/TypeScript with a **structural
  scanner** that tracks strings, template literals, comments and brace depth.
  There is no JavaScript grammar in the standard library, so this is an
  approximation — a deliberate one, and it is labelled: files it handles are
  stored with ``ParseStatus.PARTIAL`` and ``parser: structural-heuristic`` in
  their metadata, so no downstream claim can present a heuristic symbol as an
  AST-exact one. It is *not* a regex sweep: a regex cannot tell a ``function``
  keyword in a string from a definition, and every heuristic here is at least
  string- and comment-aware.

The language dispatch is a registry, so adding Java/Go/Rust later means adding a
parser and one table entry — no change to the engine, the indexer or the AI
debugger (§10).
"""

from __future__ import annotations

import abc
import ast
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from app.models.code import CodeSymbolType, ParseStatus, ReferenceKind

#: JS/TS files are parsed heuristically, so they are recorded as PARTIAL and
#: the exact strategy is stored on the file row.
#: Version of the parsing contract: symbol names, line ranges, reference kinds.
#: Bump it whenever a parser change would produce *different rows for the same
#: source*, because the indexer uses it to decide whether an existing snapshot
#: can be reused as-is or must be rebuilt from the source (§55). Without a
#: version, "already indexed" and "indexed correctly by today's parser" are the
#: same statement, and a parser fix would silently never reach an existing
#: snapshot.
PARSER_VERSION = "1"

HEURISTIC_PARSER_NOTE = "structural-heuristic"


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------
@dataclass
class ParsedSymbol:
    """One definition found in a file."""

    name: str
    qualified_name: str
    symbol_type: CodeSymbolType
    start_line: int
    end_line: int
    signature: Optional[str] = None
    documentation: Optional[str] = None
    source: Optional[str] = None
    #: Index into ``ParsedFile.symbols`` of the enclosing definition, if any.
    parent_index: Optional[int] = None
    complexity: int = 0
    is_async: bool = False
    route: Optional[str] = None
    http_method: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ParsedReference:
    """One occurrence of a name at a location."""

    name: str
    kind: ReferenceKind
    line: int
    column: Optional[int] = None
    #: Index into ``ParsedFile.symbols`` of the definition containing it.
    enclosing_index: Optional[int] = None
    #: ``IMPORTS``-style hint: the module the name came from, when the syntax
    #: makes that explicit. Used by the resolver to disambiguate common names.
    module_hint: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ParsedFile:
    """Everything one parse produced."""

    path: str
    language: str
    line_count: int = 0
    status: ParseStatus = ParseStatus.PARSED
    symbols: list[ParsedSymbol] = field(default_factory=list)
    references: list[ParsedReference] = field(default_factory=list)
    #: Imported module paths / package names, for the code graph's IMPORTS edges
    #: and for cross-file resolution hints.
    imports: list[str] = field(default_factory=list)
    #: Routes declared in this file, as ``("GET /checkout", handler_index)``.
    routes: list[tuple[str, Optional[int]]] = field(default_factory=list)
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Heuristics shared by both parsers
# ---------------------------------------------------------------------------
#: Verbs that only a data layer uses. Enough on their own to call a call a
#: database operation, whatever the receiver is named.
STRONG_DATABASE_VERBS = frozenset(
    {
        "execute",
        "executemany",
        "fetchone",
        "fetchall",
        "fetchmany",
        "commit",
        "rollback",
        "flush",
        "scalar",
        "scalars",
        "bulk_save",
    }
)

#: Verbs shared with non-data APIs (``requests.get``, ``fetchall`` vs
#: ``cache.get``). Only counted as a database operation when the receiver looks
#: like a data handle — otherwise an outbound HTTP call would be reported as a
#: query against our own store, which reads as the opposite diagnosis.
AMBIGUOUS_DATABASE_VERBS = frozenset(
    {
        "query",
        "filter",
        "filter_by",
        "select",
        "insert",
        "update",
        "delete",
        "get",
        "save",
        "find",
        "findone",
        "find_one",
        "aggregate",
        "count",
        "raw",
    }
)

#: Receiver names that mark a data handle (ORM session, cursor, repository).
DATABASE_RECEIVERS = frozenset(
    {
        "db",
        "database",
        "session",
        "cursor",
        "conn",
        "connection",
        "engine",
        "orm",
        "repository",
        "repo",
        "queryset",
        "prisma",
        "knex",
        "sequelize",
        "entitymanager",
        "datastore",
        "store",
    }
)

#: Modules / names that indicate a call leaving the process (§44 signal).
EXTERNAL_CALL_MODULES = frozenset(
    {
        "requests",
        "httpx",
        "aiohttp",
        "urllib",
        "urllib3",
        "http",
        "socket",
        "grpc",
        "boto3",
        "botocore",
        "stripe",
        "twilio",
        "axios",
        "fetch",
        "superagent",
        "got",
        "httpclient",
    }
)

#: Decorator/attribute names that mark an HTTP handler, mapped to the HTTP
#: method they imply. Used to give ROUTE symbols a real ``GET /path`` label.
ROUTE_ATTRIBUTES = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
    "head": "HEAD",
    "options": "OPTIONS",
    "route": "ANY",
    "websocket": "WS",
}

#: Route receivers: only an object that looks like a framework app/router is
#: treated as declaring a route, so ``cache.get('k')`` is not a ``GET /k``.
ROUTE_RECEIVERS = frozenset(
    {
        "app",
        "router",
        "api",
        "server",
        "application",
        "blueprint",
        "bp",
        "route",
        "routes",
        "controller",
        "fastapi",
        "express",
    }
)


def _is_external_call(root: str, dotted: str) -> bool:
    """Whether a call leaves this process (§44 external-API signal)."""
    if root in EXTERNAL_CALL_MODULES:
        return True
    lowered = dotted.lower()
    return bool(
        re.search(
            r"\b(https?client|apiclient|api_client|requests|httpx|axios)\b", lowered
        )
    )


def _is_database_call(root: str, leaf: str) -> bool:
    """Whether a call talks to a data store.

    A strong verb is enough on its own; an ambiguous one needs a data-shaped
    receiver. The distinction exists because ``get``/``find``/``count`` are
    extremely common method names, and a false "database operation" label sends
    an investigation looking at the wrong layer.
    """
    if leaf in STRONG_DATABASE_VERBS:
        return True
    if root in DATABASE_RECEIVERS:
        return True
    return leaf in AMBIGUOUS_DATABASE_VERBS and any(
        token in DATABASE_RECEIVERS for token in (root,)
    )


def _normalise_route(path: str) -> str:
    """Canonical route path: leading slash, no trailing slash, no query string."""
    cleaned = (path or "").split("?", 1)[0].strip()
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    if len(cleaned) > 1 and cleaned.endswith("/"):
        cleaned = cleaned.rstrip("/") or "/"
    return cleaned


def _complexity_from_counts(branches: int) -> int:
    """Cyclomatic-style complexity: branch points + 1.

    A *signal* for prioritising inspection (§44), never a quality judgement: a
    100-line switch is not "worse code" than a 3-line one, it is merely a place
    where a behaviour change has more places to hide.
    """
    return branches + 1


# ---------------------------------------------------------------------------
# Parser interface
# ---------------------------------------------------------------------------
class CodeParser(abc.ABC):
    """Language-specific extraction of definitions and occurrences."""

    languages: frozenset[str] = frozenset()

    @abc.abstractmethod
    def parse(self, path: str, source: str) -> ParsedFile:
        """Parse ``source``; must never raise for malformed input."""


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------
class _PythonVisitor(ast.NodeVisitor):
    """Collects definitions in document order, with parent links."""

    def __init__(self, path: str, source: str, lines: list[str]) -> None:
        self.path = path
        self.source = source
        self.lines = lines
        self.result = ParsedFile(path=path, language="python", line_count=len(lines))
        #: Stack of symbol indices, used to parent nested definitions.
        self._stack: list[int] = []
        #: ``id()`` of decorator call nodes, so ``visit_Call`` can tell a route
        #: *decorator* from a route *registration* statement.
        self._decorator_calls: set[int] = set()

    # -- helpers -----------------------------------------------------------
    def _segment(self, node: ast.AST) -> str:
        start = max(getattr(node, "lineno", 1) - 1, 0)
        end = getattr(node, "end_lineno", start + 1) or start + 1
        return "\n".join(self.lines[start:end])

    def _add(
        self,
        *,
        name: str,
        symbol_type: CodeSymbolType,
        node: ast.AST,
        signature: Optional[str] = None,
        documentation: Optional[str] = None,
        is_async: bool = False,
        complexity: int = 0,
        metadata: Optional[dict] = None,
    ) -> int:
        parent = self._stack[-1] if self._stack else None
        index = len(self.result.symbols)
        self.result.symbols.append(
            ParsedSymbol(
                name=name,
                qualified_name=self._qualify(parent, name, symbol_type),
                symbol_type=symbol_type,
                start_line=getattr(node, "lineno", 1),
                end_line=getattr(node, "end_lineno", getattr(node, "lineno", 1)) or 1,
                signature=signature,
                documentation=documentation,
                source=self._segment(node),
                parent_index=parent,
                complexity=complexity,
                is_async=is_async,
                metadata={**(metadata or {}), "parser": "python-ast"},
            )
        )
        return index

    def _qualify(
        self, parent: Optional[int], name: str, symbol_type: CodeSymbolType
    ) -> str:
        """Build the human-addressable identity used by evidence and the viewer.

        ``path`` for the module itself, ``path:Name`` for a top-level definition,
        ``path:Class.method`` for a nested one — so an evidence reference reads
        as a location a person could open, not as an opaque id.
        """
        if symbol_type is CodeSymbolType.MODULE:
            return self.path
        if parent is None:
            return f"{self.path}:{name}"
        parent_symbol = self.result.symbols[parent]
        if parent_symbol.symbol_type is CodeSymbolType.MODULE:
            return f"{self.path}:{name}"
        return f"{parent_symbol.qualified_name}.{name}"

    def _decorators(self, node: ast.AST) -> list[str]:
        names: list[str] = []
        for decorator in getattr(node, "decorator_list", []) or []:
            dotted = _dotted_name(decorator)
            if dotted:
                names.append(dotted)
        return names

    def _route_from_decorators(
        self, decorators: list[str], node: ast.AST
    ) -> tuple[Optional[str], Optional[str]]:
        """Extract ``("GET /checkout", "GET")`` from route decorators.

        Reads the decorator's own source so the literal path is taken from code,
        not guessed from the function name.
        """
        for decorator_node in getattr(node, "decorator_list", []) or []:
            if not isinstance(decorator_node, ast.Call):
                continue
            dotted = _dotted_name(decorator_node.func)
            if not dotted:
                continue
            segments = dotted.split(".")
            attribute = segments[-1].lower()
            if attribute not in ROUTE_ATTRIBUTES:
                continue
            receiver = segments[0].lower() if len(segments) > 1 else ""
            if receiver and receiver not in ROUTE_RECEIVERS:
                continue
            if not decorator_node.args:
                continue
            first = decorator_node.args[0]
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                continue
            method = ROUTE_ATTRIBUTES[attribute]
            return f"{method} {_normalise_route(first.value)}", method
        return None, None

    def _docstring(self, node: ast.AST) -> Optional[str]:
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            #: Not every node *has* a docstring; asking is a type error and the
            #: answer is always the same.
            return None
        try:
            doc = ast.get_docstring(node, clean=True)
        except TypeError:  # pragma: no cover - defensive
            return None
        return doc[:2000] if doc else None

    # -- visitors ----------------------------------------------------------
    def visit_Module(self, node: ast.Module) -> None:  # noqa: N802 - ast API
        module_index = self._add(
            name=self.path,
            symbol_type=CodeSymbolType.MODULE,
            node=node,
            documentation=self._docstring(node),
            metadata={"module_path": self.path},
        )
        self._stack.append(module_index)
        self.generic_visit(node)
        self._stack.pop()

    def _definition_common(
        self, node: ast.AST, name: str, kind: CodeSymbolType, is_async: bool
    ) -> None:
        decorators = self._decorators(node)
        #: The decorator *call* nodes are about to be visited as ordinary calls.
        #: ``@router.post("/checkout")`` is one route, declared once — without
        #: this the file would report it twice and the graph would carry two
        #: HANDLES_ROUTE edges for the same declaration.
        for decorator_node in getattr(node, "decorator_list", []) or []:
            if isinstance(decorator_node, ast.Call):
                self._decorator_calls.add(id(decorator_node))
        route, method = self._route_from_decorators(decorators, node)
        branches = _count_python_branches(node)
        symbol_index = self._add(
            name=name,
            symbol_type=CodeSymbolType.ROUTE if route else kind,
            node=node,
            signature=_python_signature(node, name),
            documentation=self._docstring(node),
            is_async=is_async,
            complexity=_complexity_from_counts(branches),
            metadata={
                "decorators": decorators,
                **({"route": route, "http_method": method} if route else {}),
            },
        )
        self.result.symbols[symbol_index].route = route
        self.result.symbols[symbol_index].http_method = method
        if route:
            self.result.routes.append((route, symbol_index))
        self._stack.append(symbol_index)
        for decorator_node in getattr(node, "decorator_list", []) or []:
            self._reference(decorator_node, ReferenceKind.DECORATOR, symbol_index)
        #: Dispatch every child through ``visit`` so the specialised visitors run.
        #: ``generic_visit(child)`` would visit the child's *children* and skip
        #: ``visit_Try``/``visit_Call`` for the child itself, which silently lost
        #: every caught exception and every route registered as a call.
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        kind = (
            CodeSymbolType.METHOD if self._inside_class() else CodeSymbolType.FUNCTION
        )
        self._definition_common(node, node.name, kind, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        kind = (
            CodeSymbolType.METHOD if self._inside_class() else CodeSymbolType.FUNCTION
        )
        self._definition_common(node, node.name, kind, is_async=True)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        bases = [_dotted_name(base) for base in node.bases]
        self._stack.append(
            self._add(
                name=node.name,
                symbol_type=CodeSymbolType.CLASS,
                node=node,
                signature=f"class {node.name}({', '.join(b for b in bases if b)})",
                documentation=self._docstring(node),
                complexity=_complexity_from_counts(_count_python_branches(node)),
                metadata={
                    "decorators": self._decorators(node),
                    "bases": [b for b in bases if b],
                },
            )
        )
        for base in node.bases:
            self._reference(base, ReferenceKind.TYPE, self._stack[-1])
        self._class_depth = getattr(self, "_class_depth", 0) + 1
        for statement in node.body:
            self.visit(statement)
        self._class_depth -= 1
        self._stack.pop()

    def _inside_class(self) -> bool:
        return getattr(self, "_class_depth", 0) > 0

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        enclosing = self._stack[-1] if self._stack else None
        for alias in node.names:
            self.result.imports.append(alias.name)
            self.result.references.append(
                ParsedReference(
                    name=alias.asname or alias.name.split(".")[0],
                    kind=ReferenceKind.IMPORT,
                    line=node.lineno,
                    column=node.col_offset,
                    enclosing_index=enclosing,
                    module_hint=alias.name,
                    metadata={"module": alias.name, "imported_as": alias.asname},
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        enclosing = self._stack[-1] if self._stack else None
        module = node.module or ""
        #: Relative imports are recorded with their dot prefix so resolution can
        #: treat them as same-package rather than guessing an absolute module.
        qualified = "." * (node.level or 0) + module
        if module:
            self.result.imports.append(module)
        for alias in node.names:
            self.result.references.append(
                ParsedReference(
                    name=alias.asname or alias.name,
                    kind=ReferenceKind.IMPORT,
                    line=node.lineno,
                    column=node.col_offset,
                    enclosing_index=enclosing,
                    module_hint=qualified,
                    metadata={
                        "module": qualified,
                        "original": alias.name,
                        "imported_as": alias.asname,
                    },
                )
            )

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        self._maybe_value_symbol(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if node.value is not None:
            self._maybe_value_symbol(node)
        self.generic_visit(node)

    def _maybe_value_symbol(self, node: ast.AST) -> None:
        """Record module/class-level assignments as (C)ONSTANT/VARIABLE symbols.

        Configuration values matter for Phase 6 — a timeout constant is exactly
        the kind of thing a change-hypothesis points at — so they are symbols
        rather than invisible assignments. Locals inside functions are skipped:
        they are not addressable from outside and would bury the real ones.
        """
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:  # pragma: no cover - exhaustive by construction
            return
        in_function = self._inside_function()
        if in_function:
            return
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            name = target.id
            is_constant = name.isupper()
            self._add(
                name=name,
                symbol_type=(
                    CodeSymbolType.CONSTANT if is_constant else CodeSymbolType.VARIABLE
                ),
                node=node,
                signature=None,
                metadata={"constant": is_constant},
            )

    def _inside_function(self) -> bool:
        if not self._stack:
            return False
        current = self.result.symbols[self._stack[-1]]
        return current.symbol_type in (CodeSymbolType.FUNCTION, CodeSymbolType.METHOD)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        enclosing = self._stack[-1] if self._stack else None
        dotted = _dotted_name(node.func)
        if dotted:
            self._reference(node.func, ReferenceKind.CALL, enclosing, name=dotted)
            self._flag_call_kinds(dotted, enclosing, node.lineno)
            self._maybe_route_call(node, dotted, enclosing)
        self.generic_visit(node)

    def _maybe_route_call(
        self, node: ast.Call, dotted: str, enclosing: Optional[int]
    ) -> None:
        """FastAPI ``@app.get`` is a decorator; Flask/Starlette ``app.add_url_rule``
        and ``app.route`` calls are not. Only recognised receivers count, so
        ``cache.get('k')`` never becomes a route."""
        if id(node) in self._decorator_calls:
            #: A decorator declares its route through the definition it decorates
            #: (``_definition_common``), which has the handler symbol to attach it
            #: to. Counting it again here would duplicate the declaration.
            return
        segments = dotted.split(".")
        if len(segments) < 2:
            return
        attribute = segments[-1].lower()
        receiver = segments[-2].lower() if len(segments) >= 2 else ""
        if attribute not in ROUTE_ATTRIBUTES or receiver not in ROUTE_RECEIVERS:
            return
        if not node.args:
            return
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            return
        route = f"{ROUTE_ATTRIBUTES[attribute]} {_normalise_route(first.value)}"
        self.result.routes.append((route, enclosing))
        if enclosing is not None:
            symbol = self.result.symbols[enclosing]
            if not symbol.route:
                symbol.route = route
                symbol.http_method = ROUTE_ATTRIBUTES[attribute]
                symbol.symbol_type = CodeSymbolType.ROUTE

    def _flag_call_kinds(
        self, dotted: str, enclosing: Optional[int], line: int
    ) -> None:
        if enclosing is None:
            return
        metadata = self.result.symbols[enclosing].metadata
        segments = dotted.split(".")
        leaf = segments[-1].lower()
        root = segments[0].lower()
        #: Ordering and the strong/weak split both matter. ``requests.get``
        #: shares the leaf ``get`` with ``session.get``, so classifying by leaf
        #: alone labelled an outbound HTTP call a database query — and the two
        #: mean opposite things when reading an incident (one is our store being
        #: slow, the other is a third party being slow).
        if _is_external_call(root, dotted):
            if dotted not in metadata.setdefault("external_api_calls", []):
                metadata["external_api_calls"].append(dotted)
        elif _is_database_call(root, leaf):
            if dotted not in metadata.setdefault("database_operations", []):
                metadata["database_operations"].append(dotted)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        enclosing = self._stack[-1] if self._stack else None
        self.result.references.append(
            ParsedReference(
                name=node.attr,
                kind=ReferenceKind.ATTRIBUTE,
                line=node.lineno,
                column=node.col_offset,
                enclosing_index=enclosing,
            )
        )
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:  # noqa: N802
        if node.exc is not None:
            enclosing = self._stack[-1] if self._stack else None
            self._reference(node.exc, ReferenceKind.CALL, enclosing)
            if enclosing is not None:
                metadata = self.result.symbols[enclosing].metadata
                name = _dotted_name(node.exc) or "raise"
                metadata.setdefault("raises", []).append(name)
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:  # noqa: N802
        enclosing = self._stack[-1] if self._stack else None
        if enclosing is not None:
            for handler in node.handlers:
                if handler.type is not None:
                    caught = _dotted_name(handler.type) or "Exception"
                    self.result.symbols[enclosing].metadata.setdefault(
                        "catches", []
                    ).append(caught)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        self.generic_visit(node)

    def _reference(
        self,
        node: ast.AST,
        kind: ReferenceKind,
        enclosing: Optional[int],
        name: Optional[str] = None,
    ) -> None:
        resolved_name = name or _dotted_name(node)
        if not resolved_name:
            return
        self.result.references.append(
            ParsedReference(
                name=resolved_name,
                kind=kind,
                line=getattr(node, "lineno", 1),
                column=getattr(node, "col_offset", None),
                enclosing_index=enclosing,
            )
        )


def _dotted_name(node: ast.AST) -> Optional[str]:
    """Reconstruct ``a.b.c`` from a Name/Attribute chain (``None`` otherwise)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _dotted_name(node.func)
    if isinstance(node, ast.Subscript):
        return _dotted_name(node.value)
    return None


def _python_signature(node: ast.AST, name: str) -> Optional[str]:
    """A readable one-line signature, built from the real argument list."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    try:
        rendered = ast.unparse(node.args)
    except Exception:  # pragma: no cover - unparse is stable in 3.9+
        rendered = "..."
    returns = ""
    annotation = getattr(node, "returns", None)
    if annotation is not None:
        try:
            returns = f" -> {ast.unparse(annotation)}"
        except Exception:  # pragma: no cover
            returns = ""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {name}({rendered}){returns}"


def _count_python_branches(node: ast.AST) -> int:
    """Count branch points, excluding nested definitions.

    Excluding nested definitions matters: a method's complexity must not include
    the complexity of the functions it happens to contain, or a thin wrapper
    around a heavy helper would be flagged as complex.
    """
    count = 0
    stack = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        if isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) and (current is not node):
            continue
        if isinstance(
            current,
            (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.ExceptHandler,
                ast.With,
                ast.AsyncWith,
                ast.IfExp,
                ast.BoolOp,
                ast.comprehension,
                ast.Assert,
                ast.Raise,
            ),
        ):
            count += 1
        if isinstance(current, ast.Match):
            count += len(current.cases)
        stack.extend(ast.iter_child_nodes(current))
    return count


class PythonParser(CodeParser):
    """Exact parser for Python, backed by the CPython AST."""

    languages = frozenset({"python"})

    def parse(self, path: str, source: str) -> ParsedFile:
        lines = source.splitlines()
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            return ParsedFile(
                path=path,
                language="python",
                line_count=len(lines),
                status=ParseStatus.FAILED,
                error=f"SyntaxError: {exc.msg} (line {exc.lineno})",
                metadata={"parser": "python-ast"},
            )
        except (ValueError, RecursionError, MemoryError) as exc:
            return ParsedFile(
                path=path,
                language="python",
                line_count=len(lines),
                status=ParseStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                metadata={"parser": "python-ast"},
            )
        visitor = _PythonVisitor(path, source, lines)
        try:
            visitor.visit(tree)
        except RecursionError:
            return ParsedFile(
                path=path,
                language="python",
                line_count=len(lines),
                status=ParseStatus.PARTIAL,
                symbols=visitor.result.symbols,
                references=visitor.result.references,
                error="file is nested too deeply to parse completely",
                metadata={"parser": "python-ast"},
            )
        return visitor.result


# ---------------------------------------------------------------------------
# JavaScript / TypeScript (structural scanner)
# ---------------------------------------------------------------------------
_JS_TOKEN_PATTERN = re.compile(
    r"""
    (?P<line_comment>//[^\n]*)
  | (?P<block_comment>/\*.*?\*/)
  | (?P<template>`(?:\\.|[^`\\])*`)
  | (?P<string>'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*")
  | (?P<ident>[A-Za-z_$][A-Za-z0-9_$]*)
  | (?P<number>\d[\d_]*(?:\.\d+)?)
  | (?P<op>=>|===|!==|==|!=|<=|>=|&&|\|\||[{}()\[\];,.:?=<>+\-*/%!&|^~@#])
    """,
    re.VERBOSE | re.DOTALL,
)


class _JsScanner:
    """A token walker over JS/TS that can see structure, not just text.

    Two jobs, both of which a line-based regex gets wrong:

    * comments and string literals are removed at *token* level, so a comment
      containing ``function fake()`` cannot become a symbol, and a template
      literal containing ``}`` cannot unbalance the brace map;
    * braces are matched into pairs up front, so a class's body is an exact token
      *range* rather than a running depth that drifts whenever the scanner
      mis-counts one brace. Mis-attributing a method to the wrong class produces
      a confidently wrong call graph, which is worse than producing none.
    """

    def __init__(self, path: str, source: str) -> None:
        self.path = path
        self.source = source
        self.lines = source.splitlines()
        self.tokens: list[tuple[str, str, int]] = []
        self.brace_pairs: dict[int, int] = {}
        self._tokenise()
        self._match_braces()

    def _tokenise(self) -> None:
        line = 1
        previous_end = 0
        for match in _JS_TOKEN_PATTERN.finditer(self.source):
            text = match.group(0)
            kind = match.lastgroup or "op"
            #: ``finditer`` skips characters between matches, so newlines in the
            #: gaps must be counted too or every line number after a gap is wrong.
            line += self.source[previous_end : match.start()].count("\n")
            previous_end = match.end()
            line += text.count("\n") if kind in ("line_comment", "block_comment") else 0
            if kind in ("line_comment", "block_comment"):
                continue
            self.tokens.append((kind, text, line))
            line += 0 if kind in ("line_comment", "block_comment") else text.count("\n")

    def _match_braces(self) -> None:
        stack: list[int] = []
        for index, (_kind, text, _line) in enumerate(self.tokens):
            if text == "{":
                stack.append(index)
            elif text == "}":
                if stack:
                    self.brace_pairs[stack.pop()] = index


class JavaScriptParser(CodeParser):
    """Structural (not AST-exact) parser for JavaScript and TypeScript."""

    languages = frozenset({"javascript", "typescript"})

    #: Keywords that can be followed by ``(`` without being a call: an arrow
    #: parameter list, an operator, a control-flow head. Without this, the
    #: ``async`` in ``async (req, res) =>`` was recorded as a function call,
    #: which puts a call site in the graph that no code ever makes.
    _NOT_CALL_KEYWORDS = frozenset(
        {
            "async",
            "await",
            "typeof",
            "new",
            "delete",
            "return",
            "if",
            "for",
            "while",
            "switch",
            "catch",
            "do",
            "else",
            "case",
            "throw",
            "in",
            "of",
            "instanceof",
            "yield",
            "void",
            "function",
            "class",
            "extends",
            "export",
            "import",
            "const",
            "let",
            "var",
        }
    )

    #: Keywords that can precede a function name and must not be mistaken for it.
    _RESERVED = frozenset(
        {
            "if",
            "for",
            "while",
            "switch",
            "catch",
            "return",
            "typeof",
            "new",
            "delete",
            "await",
            "yield",
            "in",
            "of",
            "instanceof",
            "do",
            "else",
            "case",
            "throw",
            "function",
            "class",
            "const",
            "let",
            "var",
            "export",
            "default",
            "async",
            "static",
            "public",
            "private",
            "protected",
            "abstract",
            "readonly",
            "get",
            "set",
            "interface",
            "type",
            "enum",
            "namespace",
            "declare",
        }
    )

    def __init__(self, language: str = "javascript") -> None:
        self._language = language

    def parse(self, path: str, source: str) -> ParsedFile:
        language = language_for_path(path) or self._language
        result = ParsedFile(
            path=path,
            language=language,
            line_count=source.count("\n") + (1 if source else 0),
            #: Heuristic: recorded as PARTIAL so nothing downstream can present a
            #: scanned symbol with the authority of an AST node.
            status=ParseStatus.PARTIAL,
            metadata={"parser": HEURISTIC_PARSER_NOTE},
        )
        try:
            scanner = _JsScanner(path, source)
        except (MemoryError, RecursionError) as exc:  # pragma: no cover - defensive
            result.status = ParseStatus.FAILED
            result.error = f"{type(exc).__name__}: {exc}"
            return result

        module_index = len(result.symbols)
        result.symbols.append(
            ParsedSymbol(
                name=path,
                qualified_name=path,
                symbol_type=CodeSymbolType.MODULE,
                start_line=1,
                end_line=max(len(scanner.lines), 1),
                metadata={"parser": HEURISTIC_PARSER_NOTE},
            )
        )
        self._scan(scanner, result, module_index)
        #: A file with no definitions at all (a barrel re-export, a config
        #: object) is legitimately symbol-free; still PARTIAL, never FAILED.
        result.metadata["symbol_count"] = max(len(result.symbols) - 1, 0)
        return result

    # -- scanning ----------------------------------------------------------
    def _scan(self, scanner: _JsScanner, result: ParsedFile, module_index: int) -> None:
        tokens = scanner.tokens
        lines = scanner.lines
        #: Innermost enclosing class, as ``(symbol_index, last_token_index)``. The
        #: end index comes from the pre-matched brace map, so a class scope ends
        #: exactly where its body ends rather than where a running counter happens
        #: to be — a mis-attributed method corrupts the call graph silently.
        class_stack: list[tuple[int, int]] = []
        #: ``@Controller('orders')`` prefixes — NestJS puts the route's mount
        #: point on the class, so a method's route is only correct once the two
        #: are joined. Held per-scan (not on the parser) because the registry
        #: reuses one parser instance across files.
        controller_prefixes: dict[int, str] = {}

        def enclosing() -> int:
            return class_stack[-1][0] if class_stack else module_index

        def source_for(start_line: int, end_line: int) -> str:
            return "\n".join(lines[max(start_line - 1, 0) : end_line])

        index = 0
        total = len(tokens)
        while index < total:
            kind, text, line = tokens[index]
            while class_stack and index > class_stack[-1][1]:
                class_stack.pop()

            # -- imports ---------------------------------------------------
            if text == "import" and kind == "ident":
                module = self._read_import(tokens, index)
                if module:
                    result.imports.append(module)
                    result.references.append(
                        ParsedReference(
                            name=module,
                            kind=ReferenceKind.IMPORT,
                            line=line,
                            enclosing_index=module_index,
                            module_hint=module,
                            metadata={"module": module},
                        )
                    )

            # -- definitions ----------------------------------------------
            if kind == "ident" and text in ("function", "class"):
                name_token = _next_ident(tokens, index + 1)
                if name_token and name_token[1] not in self._RESERVED:
                    start = line
                    end = _block_end_line(tokens, index)
                    is_async = (
                        index > 0
                        and tokens[index - 1][1] == "async"
                        and tokens[index - 1][0] == "ident"
                    )
                    if text == "class":
                        result.symbols.append(
                            ParsedSymbol(
                                name=name_token[1],
                                qualified_name=f"{result.path}:{name_token[1]}",
                                symbol_type=CodeSymbolType.CLASS,
                                start_line=start,
                                end_line=end,
                                signature=f"class {name_token[1]}",
                                source=source_for(start, end),
                                complexity=_count_js_branches(tokens, index, end),
                                metadata={"parser": HEURISTIC_PARSER_NOTE},
                            )
                        )
                        #: The body is *descended into* so its methods are found;
                        #: the scope pops itself once the cursor leaves the body.
                        symbol_index = len(result.symbols) - 1
                        controller_prefixes[symbol_index] = _controller_prefix(
                            tokens, index
                        )
                        class_stack.append(
                            (symbol_index, _block_close_index(scanner, index))
                        )
                        index += 1
                        continue
                    else:
                        parent = enclosing()
                        sym = ParsedSymbol(
                            name=name_token[1],
                            qualified_name=self._qualify(result, parent, name_token[1]),
                            symbol_type=(
                                CodeSymbolType.METHOD
                                if class_stack
                                else CodeSymbolType.FUNCTION
                            ),
                            start_line=start,
                            end_line=end,
                            signature=f"{'async ' if is_async else ''}function {name_token[1]}",
                            source=source_for(start, end),
                            parent_index=parent if class_stack else module_index,
                            is_async=is_async,
                            complexity=_count_js_branches(tokens, index, end),
                            metadata={"parser": HEURISTIC_PARSER_NOTE},
                        )
                        result.symbols.append(sym)
                    #: A function body is skipped: its own source range already
                    #: covers any inner callback, and promoting an inner arrow
                    #: callback to a definition would flood the index with
                    #: anonymous handlers no trace can ever address.
                    index = _after_block(scanner, index)
                    continue

            # -- arrow / function expressions assigned to a name -----------
            if kind == "ident" and text in ("const", "let", "var", "export"):
                arrow = self._read_arrow_definition(
                    scanner, index, result, enclosing(), source_for
                )
                if arrow is not None:
                    index = arrow
                    continue

            # -- interface / type declarations ----------------------------
            if kind == "ident" and text in ("interface", "type", "enum"):
                name_token = _next_ident(tokens, index + 1)
                if name_token:
                    end = _block_end_line(tokens, index)
                    result.symbols.append(
                        ParsedSymbol(
                            name=name_token[1],
                            qualified_name=f"{result.path}:{name_token[1]}",
                            symbol_type=CodeSymbolType.INTERFACE,
                            start_line=line,
                            end_line=end,
                            signature=f"{text} {name_token[1]}",
                            source=source_for(line, end),
                            parent_index=module_index,
                            metadata={
                                "parser": HEURISTIC_PARSER_NOTE,
                                "declaration": text,
                            },
                        )
                    )

            # -- class methods: ``name(args) {`` inside a class body --------
            if kind == "ident" and class_stack and index + 1 < total:
                next_kind, next_text, _ = tokens[index + 1]
                if next_text == "(" and text not in self._RESERVED:
                    closing = _matching_paren(tokens, index + 1)
                    after = (
                        tokens[closing + 1]
                        if closing is not None and closing + 1 < total
                        else None
                    )
                    if after and after[1] == "{" and closing is not None:
                        start = line
                        end = _block_end_line(tokens, closing)
                        #: ``class_stack`` entries are ``(symbol_index, end_token)``;
                        #: only the symbol index is the parent.
                        parent = class_stack[-1][0]
                        is_async = index > 0 and tokens[index - 1][1] == "async"
                        symbol = ParsedSymbol(
                            name=text,
                            qualified_name=self._qualify(result, parent, text),
                            symbol_type=CodeSymbolType.METHOD,
                            start_line=start,
                            end_line=end,
                            signature=f"{'async ' if is_async else ''}{text}()",
                            source=source_for(start, end),
                            parent_index=parent,
                            is_async=is_async,
                            complexity=_count_js_branches(tokens, index, end),
                            metadata={"parser": HEURISTIC_PARSER_NOTE},
                        )
                        self._maybe_route_call(
                            result,
                            tokens,
                            index,
                            symbol,
                            len(result.symbols),
                            controller_prefixes.get(class_stack[-1][0], ""),
                        )
                        result.symbols.append(symbol)
                        index = _after_block(scanner, index)
                        continue

            # -- calls / route registrations -------------------------------
            if (
                kind == "ident"
                and text not in self._NOT_CALL_KEYWORDS
                and index + 1 < total
                and tokens[index + 1][1] == "("
            ):
                path_literal = _string_literal(tokens, index + 2)
                #: Decorator-style registrations (``@Get(':id')``) are handled by
                #: the method branch, which knows the decorated symbol. Handling
                #: them here too attached a *method's* route to its class.
                is_decorator = index > 0 and tokens[index - 1][1] == "@"
                if not is_decorator and self._looks_like_route(
                    tokens, index, path_literal
                ):
                    method = ROUTE_ATTRIBUTES.get(text.lower())
                    if method:
                        route = f"{method} {_normalise_route(path_literal or '')}"
                        #: ``app.get('/x', handler)``: when the handler is a
                        #: named symbol, the route is attached to *it* rather
                        #: than to the file, which is what lets a trace that hit
                        #: ``/x`` resolve to the function that served it.
                        handler_index = self._route_handler_index(
                            result, tokens, index, enclosing()
                        )
                        result.routes.append((route, handler_index))
                        symbol = result.symbols[handler_index]
                        if (
                            symbol.symbol_type is not CodeSymbolType.CLASS
                            and not symbol.route
                        ):
                            symbol.route = route
                            symbol.http_method = method
                result.references.append(
                    ParsedReference(
                        name=self._dotted_call(tokens, index),
                        kind=ReferenceKind.CALL,
                        line=line,
                        enclosing_index=enclosing(),
                    )
                )
                metadata = result.symbols[enclosing()].metadata
                dotted = self._dotted_call(tokens, index)
                leaf = dotted.split(".")[-1].lower()
                root = dotted.split(".")[0].lower()
                if _is_external_call(root, dotted):
                    metadata.setdefault("external_api_calls", []).append(dotted)
                elif _is_database_call(root, leaf):
                    metadata.setdefault("database_operations", []).append(dotted)

            index += 1

    def _route_handler_index(
        self, result: ParsedFile, tokens: list, index: int, fallback: int
    ) -> int:
        """Find the symbol that serves a route registered as a call.

        Recognises ``router.<verb>('/path', handlerName)`` and inline arrows
        (which stay attributed to the enclosing scope, since an anonymous handler
        has no addressable identity). Falls back to ``fallback`` — never to a
        guess, because a wrong handler makes a trace map to the wrong function.
        """
        opening = (
            index + 1
            if index + 1 < len(tokens) and tokens[index + 1][1] == "("
            else None
        )
        if opening is None:
            return fallback
        closing = _matching_paren(tokens, opening)
        if closing is None:
            return fallback
        #: Scan the argument list for a bare identifier that names a known symbol.
        for position in range(opening + 1, closing):
            kind, text, _line = tokens[position]
            if kind != "ident" or text in self._NOT_CALL_KEYWORDS:
                continue
            if tokens[position + 1][1] == "(" if position + 1 < len(tokens) else False:
                continue
            for candidate_index, candidate in enumerate(result.symbols):
                if candidate.name == text:
                    return candidate_index
        return fallback

    def _qualify(self, result: ParsedFile, parent: Optional[int], name: str) -> str:
        """``path:Name`` at module level, ``path:Class.method`` inside a class.

        The same convention the Python parser uses, so an evidence reference
        reads identically whichever language the file happens to be in.
        """
        if parent is None or parent >= len(result.symbols):
            return f"{result.path}:{name}"
        parent_symbol = result.symbols[parent]
        if parent_symbol.symbol_type is CodeSymbolType.MODULE:
            return f"{result.path}:{name}"
        return f"{parent_symbol.qualified_name}.{name}"

    def _read_import(self, tokens, index: int) -> Optional[str]:
        for position in range(index + 1, min(index + 12, len(tokens))):
            kind, text, _line = tokens[position]
            if kind == "string":
                return text.strip("'\"`")
            if text in ("from", "require"):
                continue
            if text == ";":
                break
        return None

    def _read_arrow_definition(
        self,
        scanner: _JsScanner,
        index: int,
        result: ParsedFile,
        parent: int,
        source_for,
    ) -> Optional[int]:
        """Recognise ``const name = (…) => { }`` / ``= function`` / ``= async (…) =>``."""
        tokens = scanner.tokens
        cursor = index + 1
        #: ``export const x = …`` / ``export default function …``
        if tokens[index][1] == "export":
            if cursor < len(tokens) and tokens[cursor][1] == "default":
                cursor += 1
            if cursor >= len(tokens):
                return None
        if cursor >= len(tokens) or tokens[cursor][0] != "ident":
            return None
        name = tokens[cursor][1]
        if (
            name in self._RESERVED
            or cursor + 1 >= len(tokens)
            or tokens[cursor + 1][1] != "="
        ):
            return None
        body_start = cursor + 2
        if body_start >= len(tokens):
            return None
        is_async = tokens[body_start][1] == "async"
        if is_async:
            body_start += 1
        if body_start >= len(tokens):
            return None
        #: Only these four shapes begin a function value. An arbitrary identifier
        #: means a *call* (``const app = express()``), and treating that as a
        #: definition used to swallow the rest of the file into one bogus symbol.
        arrow = tokens[body_start][1] in ("(", "function", "class")
        if tokens[body_start][1] == "function":
            body_start += 1
        if not arrow:
            #: Configuration constants are still symbols (§12 CONSTANT), because
            #: a changed timeout is exactly what a hypothesis points at.
            if tokens[body_start][0] in ("number", "string"):
                end_line = tokens[body_start][2]
                result.symbols.append(
                    ParsedSymbol(
                        name=name,
                        qualified_name=self._qualify(result, parent, name),
                        symbol_type=(
                            CodeSymbolType.CONSTANT
                            if name.isupper() or name[0].isupper()
                            else CodeSymbolType.VARIABLE
                        ),
                        start_line=tokens[index][2],
                        end_line=end_line,
                        source=source_for(tokens[index][2], end_line),
                        parent_index=parent,
                        metadata={"parser": HEURISTIC_PARSER_NOTE},
                    )
                )
                return None
            return None
        end = _block_end_line(tokens, body_start)
        result.symbols.append(
            ParsedSymbol(
                name=name,
                qualified_name=self._qualify(result, parent, name),
                symbol_type=CodeSymbolType.FUNCTION,
                start_line=tokens[index][2],
                end_line=end,
                signature=f"{'async ' if is_async else ''}{name} = (…) =>",
                source=source_for(tokens[index][2], end),
                parent_index=parent,
                is_async=is_async,
                complexity=_count_js_branches(tokens, body_start, end),
                metadata={"parser": HEURISTIC_PARSER_NOTE},
            )
        )
        return _after_block(scanner, cursor)

    def _looks_like_route(
        self, tokens, index: int, path_literal: Optional[str]
    ) -> bool:
        """Only ``<router>.<verb>('/path')`` shapes are routes.

        The receiver check is what stops ``cache.get('key')`` from becoming a
        ``GET /key`` route — a false route poisons trace→code mapping, which is
        the feature the whole phase depends on.
        """
        if path_literal is None:
            return False
        attribute = tokens[index][1].lower()
        if attribute not in ROUTE_ATTRIBUTES:
            return False
        if index == 0:
            return False
        previous_kind, previous_text, _ = tokens[index - 1]
        if previous_text == "." and index >= 2:
            receiver = tokens[index - 2][1].lower()
            return receiver in ROUTE_RECEIVERS
        #: NestJS-style decorators: ``@Get('/path')`` / ``@Post()``.
        return previous_text == "@"

    def _dotted_call(self, tokens, index: int) -> str:
        parts = [tokens[index][1]]
        cursor = index - 1
        while (
            cursor >= 1
            and tokens[cursor][1] == "."
            and tokens[cursor - 1][0] == "ident"
        ):
            parts.insert(0, tokens[cursor - 1][1])
            cursor -= 2
        return ".".join(parts)

    def _maybe_route_call(
        self, result, tokens, index, symbol, symbol_index, prefix: str = ""
    ) -> None:
        """NestJS decorators attach a route to the method they decorate."""
        cursor = index - 2
        seen = 0
        while cursor >= 0 and seen < 8:
            text = tokens[cursor][1]
            if text == "@":
                attribute = (
                    tokens[cursor + 1][1].lower() if cursor + 1 < len(tokens) else ""
                )
                method = ROUTE_ATTRIBUTES.get(attribute)
                if method:
                    path_literal = _string_literal(tokens, cursor + 3) or ""
                    route = f"{method} {_join_route(prefix, path_literal)}"
                    symbol.route = route
                    symbol.http_method = method
                    symbol.symbol_type = CodeSymbolType.ROUTE
                    result.routes.append((route, symbol_index))
                return
            if text in ("}", ";"):
                return
            cursor -= 1
            seen += 1


def _join_route(prefix: str, path: str) -> str:
    """Join a controller mount point and a handler path into one route."""
    if not prefix:
        return _normalise_route(path)
    combined = (
        f"/{prefix.strip('/')}/{path.strip('/')}" if path else f"/{prefix.strip('/')}"
    )
    return _normalise_route(combined)


def _controller_prefix(tokens, class_token_index: int) -> str:
    """The mount point declared by ``@Controller('x')`` above a class, if any."""
    position = class_token_index - 1
    limit = max(class_token_index - 12, 0)
    while position >= limit:
        if tokens[position][1] in (";", "}"):
            return ""
        if tokens[position][1] == "@" and position + 1 < len(tokens):
            name = tokens[position + 1][1].lower()
            if name in ("controller", "route", "prefix", "mount"):
                return _string_literal(tokens, position + 3) or ""
        position -= 1
    return ""


def _next_ident(tokens, index: int) -> Optional[tuple[str, str, int]]:
    for position in range(index, min(index + 4, len(tokens))):
        kind, text, line = tokens[position]
        if kind == "ident":
            return (kind, text, line)
        if text in ("(", "=", ")"):
            return None
    return None


def _string_literal(tokens, index: int) -> Optional[str]:
    for position in range(index, min(index + 3, len(tokens))):
        kind, text, _ = tokens[position]
        if kind == "string":
            return text.strip("'\"`")
        if text in (")", ",", "}"):
            break
    return None


def _matching_paren(tokens, open_index: int) -> Optional[int]:
    depth = 0
    for position in range(open_index, len(tokens)):
        text = tokens[position][1]
        if text == "(":
            depth += 1
        elif text == ")":
            depth -= 1
            if depth == 0:
                return position
    return None


def _block_end_line(tokens, index: int) -> int:
    """Line of the closing brace of the block that starts at/after ``index``."""
    depth = 0
    started = False
    for position in range(index, len(tokens)):
        text = tokens[position][1]
        if text == "{":
            depth += 1
            started = True
        elif text == "}":
            depth -= 1
            if started and depth == 0:
                return tokens[position][2]
    return tokens[-1][2] if tokens else 1


def _block_open_index(scanner: _JsScanner, index: int) -> Optional[int]:
    """Token index of the ``{`` that opens the block at/after ``index``."""
    for position in range(index, len(scanner.tokens)):
        text = scanner.tokens[position][1]
        if text == "{":
            return position
        if text == ";":
            #: A brace-less body (``const f = () => 1;``) has no block to skip.
            return None
    return None


def _block_close_index(scanner: _JsScanner, index: int) -> int:
    """Token index of the ``}`` closing the block that opens at/after ``index``.

    Falls back to the declaration's own token when there is no block, so the
    caller can always use the result as an exclusive upper bound.
    """
    opening = _block_open_index(scanner, index)
    if opening is None:
        return index
    return scanner.brace_pairs.get(opening, opening)


def _after_block(scanner: _JsScanner, index: int) -> int:
    """Scan position just past the block that starts at/after ``index``."""
    opening = _block_open_index(scanner, index)
    if opening is None:
        return index + 1
    closing = scanner.brace_pairs.get(opening)
    return len(scanner.tokens) if closing is None else closing + 1


def _count_js_branches(tokens, start: int, end_line: int) -> int:
    """Count branch keywords between ``start`` and ``end_line``."""
    keywords = {
        "if",
        "else",
        "for",
        "while",
        "case",
        "catch",
        "&&",
        "||",
        "?",
    }
    count = 0
    for position in range(start, len(tokens)):
        kind, text, line = tokens[position]
        if line > end_line:
            break
        if text in keywords:
            count += 1
    return _complexity_from_counts(count)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_PARSERS: dict[str, CodeParser] = {}


def register_parser(parser: CodeParser) -> None:
    """Register a parser for each language it declares (§10 extensibility)."""
    for language in parser.languages:
        _PARSERS[language] = parser


register_parser(PythonParser())
register_parser(JavaScriptParser())


def parser_for_language(language: Optional[str]) -> Optional[CodeParser]:
    if not language:
        return None
    return _PARSERS.get(language)


def language_for_path(path: str) -> Optional[str]:
    #: Imported lazily to keep this module free of the provider's settings load
    #: during import (parsers are pure functions of text).
    from app.services.repository_provider import detect_language

    return detect_language(path)


def parse_source(path: str, source: str, language: Optional[str] = None) -> ParsedFile:
    """Parse ``source`` with the parser registered for its language."""
    resolved = language or language_for_path(path)
    parser = parser_for_language(resolved)
    if parser is None:
        return ParsedFile(
            path=path,
            language=resolved or "unknown",
            line_count=source.count("\n") + (1 if source else 0),
            #: Unsupported is not Failed: the file is still indexed, searchable
            #: and viewable — it simply contributes no symbols, and saying that
            #: explicitly is what keeps "no symbols found" from reading as
            #: "this file is empty".
            status=ParseStatus.UNSUPPORTED,
            metadata={"parser": None, "language": resolved},
        )
    try:
        return parser.parse(path, source)
    except Exception as exc:  # noqa: BLE001 - a parser must never break indexing
        return ParsedFile(
            path=path,
            language=resolved or "unknown",
            line_count=source.count("\n") + (1 if source else 0),
            status=ParseStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}",
            metadata={"parser": type(parser).__name__},
        )


def supported_languages() -> list[str]:
    return sorted(_PARSERS)


def dumps_parse_summary(parsed: ParsedFile) -> str:
    """Compact, JSON-serializable summary — used in index-run metadata."""
    return json.dumps(
        {
            "path": parsed.path,
            "language": parsed.language,
            "status": parsed.status.value,
            "symbols": len(parsed.symbols),
            "references": len(parsed.references),
            "imports": len(parsed.imports),
            "routes": [route for route, _ in parsed.routes],
            "error": parsed.error,
        },
        sort_keys=True,
    )
