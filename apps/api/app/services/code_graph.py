"""ARGUS Code Graph Builder (Phase 6 §13, §14, §23).

Turns parsed files into a *resolved* code graph: which definition each name
occurrence refers to, and which typed edges therefore exist between definitions.

Everything here is a **static inference**, and the module says so in three ways:

* every resolved reference records *how* it was resolved (``same-file``,
  ``import-alias``, ``unique-name``, ``unresolved``), so a caller can weight a
  guess differently from a certainty;
* every edge carries a ``confidence`` derived from that method, and edges that
  could only be guessed (a unique name match) are marked as such;
* nothing is invented. An import of a third-party package resolves to *no node*
  because the package is not in the snapshot, and an unresolved call stays
  unresolved rather than being attached to the most similar-looking function.

That last point is the one that matters most in practice. A code graph with a
few honest gaps is useful; a complete-looking graph built on name-similarity
guesses will confidently send an engineer to the wrong function, which is worse
than sending them nowhere.

Some §13 relationship types are deliberately *not* materialised as edges:

* ``DEFINES`` is ``CodeSymbol.parent_symbol_id`` — the same fact stored once.
  Repeating it as an edge would double the row count of the graph to restate a
  column, so the query layer synthesises it on read instead.
* ``HANDLES_ROUTE`` is ``CodeSymbol.route``, for the same reason.
* ``CALLS_API`` / ``READS`` / ``WRITES`` describe calls whose *target is outside
  the snapshot* (a payment provider, a table). There is no node to point at, so
  they are recorded on the calling symbol as ``external_api_calls`` /
  ``database_operations`` metadata and surfaced as risk signals — an edge to a
  node that does not exist would be a fabricated fact.
"""

from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.models.code import CodeRelationshipType, ReferenceKind
from app.services.code_parser import ParsedFile, ParsedReference, ParsedSymbol

logger = logging.getLogger(__name__)

#: Extensions tried when resolving a JavaScript/TypeScript relative import.
JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

#: Module-path segments that mark a data-access layer. Used to type a resolved
#: call as ``QUERIES`` rather than ``CALLS`` — a distinction that decides whether
#: an investigation looks at the query or at the caller.
DATA_LAYER_TOKENS = frozenset(
    {
        "repository",
        "repositories",
        "repo",
        "dao",
        "store",
        "stores",
        "queries",
        "query",
        "models",
        "model",
        "orm",
        "db",
        "database",
    }
)

#: Confidence assigned by resolution strategy. Documented in docs/phase-6.md;
#: changing a number here changes how much weight the debugger gives an edge.
CONFIDENCE_BY_RESOLUTION = {
    "same-file": 0.9,
    "same-class": 0.9,
    "import-alias": 0.8,
    "module-import": 0.8,
    "route": 0.7,
    "unique-name": 0.4,
}

#: Reference kinds worth resolving to a definition. Attribute accesses are
#: deliberately excluded: ``self.db``/``os.path`` are *occurrences* that make
#: ``find_references`` useful, but treating them as call targets produced 35k
#: "unresolved" rows on a 272-file repository and drove the resolution ratio to
#: 0.21 — a number that measured the noise floor rather than the call graph's
#: completeness. They are still stored, marked ``not-attempted``.
RESOLVABLE_KINDS = frozenset(
    {
        ReferenceKind.CALL,
        ReferenceKind.TYPE,
        ReferenceKind.DECORATOR,
        ReferenceKind.ROUTE,
        ReferenceKind.QUERY,
    }
)

#: Marked on a reference that no resolution was attempted for, so the stored
#: value distinguishes "we did not try" from "we tried and failed".
NOT_ATTEMPTED = "not-attempted"


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------
@dataclass
class ResolvedReference:
    """One reference occurrence with the definition it was resolved to."""

    file_path: str
    name: str
    reference_kind: ReferenceKind
    line: int
    column: Optional[int] = None
    enclosing_qualified: Optional[str] = None
    target_qualified: Optional[str] = None
    #: ``True`` when the target is the enclosing definition itself (recursion).
    self_reference: bool = False
    resolution: str = "unresolved"
    metadata: dict = field(default_factory=dict)


@dataclass
class ResolvedRelationship:
    """One resolved, typed edge between two definitions."""

    source_qualified: str
    target_qualified: str
    relationship_type: CodeRelationshipType
    line: int = 0
    confidence: float = 0.5
    evidence: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class CodeGraphStats:
    """Counts for the index run — cheap to compute, useful for diagnosing."""

    symbols: int = 0
    references: int = 0
    resolved_references: int = 0
    unresolved_references: int = 0
    relationships: int = 0
    by_resolution: dict = field(default_factory=dict)
    by_kind: dict = field(default_factory=dict)
    not_attempted: int = 0
    fan_out: dict = field(default_factory=dict)
    fan_in: dict = field(default_factory=dict)

    def resolution_ratio(self) -> float:
        """Share of *resolvable* occurrences that bound to a definition.

        Scoped to the kinds resolution is attempted for, so the number answers
        "how much of the call graph did we recover", not "how many identifiers
        exist in this codebase".
        """
        total = self.resolved_references + self.unresolved_references
        return (self.resolved_references / total) if total else 0.0


@dataclass
class CodeGraphResult:
    """Everything the builder produced for one snapshot."""

    references: list[ResolvedReference] = field(default_factory=list)
    relationships: list[ResolvedRelationship] = field(default_factory=list)
    stats: CodeGraphStats = field(default_factory=CodeGraphStats)


# ---------------------------------------------------------------------------
# Module index
# ---------------------------------------------------------------------------
class ModuleIndex:
    """Maps file paths to the module names other files can import them by."""

    def __init__(self, paths: Iterable[str]) -> None:
        self.paths = set(paths)
        #: module name → path, for Python-style dotted imports.
        self.by_module: dict[str, str] = {}
        #: package directory → its ``__init__.py`` path.
        self.by_package: dict[str, str] = {}
        #: basename without extension → paths, for JS bare-specifier resolution.
        self.by_stem: dict[str, list[str]] = {}
        for path in self.paths:
            self._register(path)

    def _register(self, path: str) -> None:
        if path.endswith(".py"):
            if posixpath.basename(path) == "__init__.py":
                package = posixpath.dirname(path) or "."
                self.by_package[package.replace("/", ".")] = path
                module = package.replace("/", ".")
            else:
                module = path[: -len(".py")].replace("/", ".")
            self.by_module.setdefault(module, path)
        stem = posixpath.splitext(posixpath.basename(path))[0]
        self.by_stem.setdefault(stem, []).append(path)

    def resolve_import(self, importer_path: str, module_hint: str) -> Optional[str]:
        """Resolve an import hint to a file path inside this snapshot.

        Handles the two shapes that actually occur: Python dotted/relative
        modules and JavaScript relative paths. Anything else (a third-party
        package, an absolute specifier) returns ``None`` — correctly, because
        the target is not in the snapshot.
        """
        if not module_hint:
            return None
        hint = module_hint.strip()

        if hint.startswith("."):
            return self._resolve_relative(importer_path, hint)

        if "/" in hint or hint.startswith("@") or hint == "":
            return None

        candidate = hint.replace(".", "/")
        for suffix in ("", ".py", "/__init__.py"):
            path = f"{candidate}{suffix}"
            if path in self.paths:
                return path
        #: A dotted module may itself be a package directory in the snapshot.
        if candidate in self.by_package:
            return self.by_package[candidate]
        if hint in self.by_module:
            return self.by_module[hint]
        #: Last resort for bare specifiers: a unique stem match. Only used when
        #: unambiguous, because two files named ``utils`` are extremely common
        #: and binding to the wrong one would fabricate a call edge.
        stem_matches = self.by_stem.get(hint.split(".")[-1], [])
        if len(stem_matches) == 1:
            return stem_matches[0]
        return None

    def _resolve_relative(self, importer_path: str, hint: str) -> Optional[str]:
        depth = len(hint) - len(hint.lstrip("."))
        remainder = hint[depth:].replace(".", "/")
        base = posixpath.dirname(importer_path)
        for _ in range(max(depth - 1, 0)):
            base = posixpath.dirname(base)
        target = (
            posixpath.normpath(posixpath.join(base, remainder)) if remainder else base
        )
        if target.startswith(".."):
            return None
        if target in self.paths:
            return target
        for extension in JS_EXTENSIONS:
            if f"{target}{extension}" in self.paths:
                return f"{target}{extension}"
        for extension in JS_EXTENSIONS:
            index_path = f"{target}/index{extension}"
            if index_path in self.paths:
                return index_path
        if f"{target}/__init__.py" in self.paths:
            return f"{target}/__init__.py"
        return None


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
class CodeGraphBuilder:
    """Resolves references and emits edges for a whole snapshot."""

    def __init__(self, files: list[ParsedFile]) -> None:
        self.files = files
        self.module_index = ModuleIndex(parsed.path for parsed in files)
        #: qualified_name → ParsedSymbol
        self.by_qualified: dict[str, ParsedSymbol] = {}
        #: name → [(qualified_name, symbol)]
        self.by_name: dict[str, list[tuple[str, ParsedSymbol]]] = {}
        #: path → parsed file
        self.by_path: dict[str, ParsedFile] = {}
        #: (path, name) → qualified_name for import aliases
        self.import_aliases: dict[tuple[str, str], Optional[str]] = {}
        self._module_symbol: dict[str, str] = {}
        #: qualified_name → file path, so an edge can be typed by where its
        #: target lives without a second database lookup.
        self._path_by_qualified: dict[str, str] = {}
        #: path → name → qualified names. Resolution asks "what does this name
        #: mean in this file?" once per occurrence; answering by scanning every
        #: symbol of the file made resolution quadratic in file size.
        self._names_by_path: dict[str, dict[str, list[str]]] = {}
        self._build_indexes()

    # -- indexes -----------------------------------------------------------
    def _build_indexes(self) -> None:
        for parsed in self.files:
            self.by_path[parsed.path] = parsed
            for symbol in parsed.symbols:
                qualified = symbol.qualified_name
                #: Overloads / same-named siblings: the first definition wins
                #: for resolution, and the duplicate is still stored as a symbol
                #: in its own right. Recording the collision rather than silently
                #: overwriting keeps a later "why did this resolve there?"
                #: question answerable.
                self.by_qualified.setdefault(qualified, symbol)
                self._path_by_qualified.setdefault(qualified, parsed.path)
                self.by_name.setdefault(symbol.name, []).append((qualified, symbol))
                self._names_by_path.setdefault(parsed.path, {}).setdefault(
                    symbol.name, []
                ).append(qualified)
                if symbol.symbol_type.name == "MODULE":
                    self._module_symbol[parsed.path] = qualified

        for parsed in self.files:
            for reference in parsed.references:
                if reference.kind is not ReferenceKind.IMPORT:
                    continue
                target_path = self.module_index.resolve_import(
                    parsed.path, reference.module_hint or ""
                )
                alias = reference.name
                resolved: Optional[str] = None
                if target_path:
                    exported = reference.metadata.get("original") or alias
                    resolved = self._export_of(target_path, exported)
                    if (
                        resolved is None
                        and reference.metadata.get("imported_as") is None
                    ):
                        resolved = self._export_of(target_path, alias)
                self.import_aliases[(parsed.path, alias)] = resolved

    def _export_of(self, path: str, name: str) -> Optional[str]:
        """The definition a file exports under ``name`` (module path itself for
        a module import)."""
        parsed = self.by_path.get(path)
        if parsed is None:
            return None
        if name == parsed.path or name.endswith(path):
            return self._module_symbol.get(path)
        for symbol in parsed.symbols:
            if symbol.name == name and symbol.symbol_type.name != "MODULE":
                return symbol.qualified_name
        #: ``from x import y`` where ``y`` is a submodule, not a definition.
        nested = posixpath.splitext(path)[0].replace("/", ".") + f".{name}"
        nested_path = self.module_index.by_module.get(nested)
        if nested_path:
            return self._module_symbol.get(nested_path)
        return None

    # -- resolution --------------------------------------------------------
    def _resolve_call(
        self,
        parsed: ParsedFile,
        reference: ParsedReference,
        enclosing: Optional[ParsedSymbol],
    ) -> tuple[Optional[str], str]:
        """Resolve one call occurrence to a definition, with its strategy."""
        raw = reference.name
        if not raw:
            return None, "unresolved"
        leaf = raw.split(".")[-1]
        base = raw.split(".")[0]

        if enclosing is not None:
            # 1. a sibling method of the same class
            if enclosing.parent_index is not None:
                parent = parsed.symbols[enclosing.parent_index]
                if parent.symbol_type.name == "CLASS":
                    sibling = f"{parent.qualified_name}.{leaf}"
                    if sibling in self.by_qualified:
                        return sibling, "same-class"
            # 2. a definition in the same file
            same_file = self._in_file(parsed, leaf)
            if same_file:
                return same_file, "same-file"
        else:
            same_file = self._in_file(parsed, leaf)
            if same_file:
                return same_file, "same-file"

        # 3. a name imported into this file
        aliased = self.import_aliases.get((parsed.path, base))
        if aliased:
            if len(raw.split(".")) > 1:
                #: ``mod.func()`` — bind to the *base* module and let the
                #: attribute be part of the edge evidence; binding to one of the
                #: module's functions by name would be a guess.
                return aliased, "import-alias"
            return aliased, "import-alias"

        # 4. a route registration: a handler reference matching a known route
        if base and any(
            entry for entry in parsed.routes if entry[0].endswith(f"/{base}")
        ):
            for route, index in parsed.routes:
                if index is not None and index < len(parsed.symbols):
                    symbol = parsed.symbols[index]
                    if symbol.name == leaf:
                        return symbol.qualified_name, "route"

        # 5. exactly one definition in the snapshot with that name
        matches = self.by_name.get(leaf, [])
        if len(matches) == 1:
            return matches[0][0], "unique-name"
        return None, "unresolved"

    #: Symbol types a *call* can bind to. A module, variable or constant is not
    #: callable, and allowing one to win would bind a call to, say, a config dict
    #: that happens to share a name with the function being invoked.
    CALLABLE_TYPES = frozenset(
        {"CLASS", "FUNCTION", "METHOD", "INTERFACE", "ROUTE", "HANDLER"}
    )

    def _in_file(self, parsed: ParsedFile, name: str) -> Optional[str]:
        candidates = [
            qualified
            for qualified in self._names_by_path.get(parsed.path, {}).get(name, [])
            if self.by_qualified[qualified].symbol_type.name in self.CALLABLE_TYPES
        ]
        if not candidates:
            return None
        #: Ambiguous inside one file (a method and a module-level function of the
        #: same name): the shortest qualified name is the outermost definition,
        #: which is what a bare call at module scope would bind to.
        return min(candidates, key=len)

    # -- build -------------------------------------------------------------
    def build(self) -> CodeGraphResult:
        result = CodeGraphResult()
        seen_edges: set[tuple[str, str, str, int]] = set()
        fan_out: dict[str, int] = {}
        fan_in: dict[str, int] = {}

        for parsed in sorted(self.files, key=lambda item: item.path):
            self._build_modifies_edges(parsed, result, seen_edges, fan_out, fan_in)
            for reference in parsed.references:
                enclosing = (
                    parsed.symbols[reference.enclosing_index]
                    if reference.enclosing_index is not None
                    and reference.enclosing_index < len(parsed.symbols)
                    else None
                )
                if reference.kind is ReferenceKind.IMPORT:
                    continue
                result.stats.by_kind[reference.kind.value] = (
                    result.stats.by_kind.get(reference.kind.value, 0) + 1
                )
                resolvable = reference.kind in RESOLVABLE_KINDS
                target: Optional[str] = None
                strategy = NOT_ATTEMPTED
                if resolvable:
                    target, strategy = self._resolve_call(parsed, reference, enclosing)
                else:
                    result.stats.not_attempted += 1
                self_reference = bool(
                    target and enclosing and target == enclosing.qualified_name
                )
                result.references.append(
                    ResolvedReference(
                        file_path=parsed.path,
                        name=reference.name,
                        reference_kind=reference.kind,
                        line=reference.line,
                        column=reference.column,
                        enclosing_qualified=enclosing.qualified_name
                        if enclosing
                        else None,
                        target_qualified=target,
                        self_reference=self_reference,
                        resolution=strategy,
                        metadata=dict(reference.metadata or {}),
                    )
                )
                if not resolvable:
                    continue
                if target is None:
                    result.stats.unresolved_references += 1
                    continue
                result.stats.resolved_references += 1
                result.stats.by_resolution[strategy] = (
                    result.stats.by_resolution.get(strategy, 0) + 1
                )
                if enclosing is None or self_reference:
                    #: A call with no enclosing definition (module-level) or a
                    #: recursive call adds no information to the call graph; the
                    #: occurrence is still stored as a reference above.
                    continue
                edge_type = self._edge_type_for(target)
                key = (
                    enclosing.qualified_name,
                    target,
                    edge_type.value,
                    reference.line,
                )
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                result.relationships.append(
                    ResolvedRelationship(
                        source_qualified=enclosing.qualified_name,
                        target_qualified=target,
                        relationship_type=edge_type,
                        line=reference.line,
                        confidence=CONFIDENCE_BY_RESOLUTION.get(strategy, 0.3),
                        evidence=f"{reference.kind.value} reference resolved by {strategy}",
                    )
                )
                fan_out[enclosing.qualified_name] = (
                    fan_out.get(enclosing.qualified_name, 0) + 1
                )
                fan_in[target] = fan_in.get(target, 0) + 1

            self._build_declaration_edges(parsed, result, seen_edges, fan_out, fan_in)

        result.stats.symbols = len(self.by_qualified)
        result.stats.references = len(result.references)
        result.stats.relationships = len(result.relationships)
        result.stats.fan_out = fan_out
        result.stats.fan_in = fan_in
        return result

    def _edge_type_for(self, target_qualified: str) -> CodeRelationshipType:
        """Type a resolved call by what it targets.

        A call whose target lives in a data-access module is a ``QUERIES`` edge,
        which tells an investigation "this path talks to the store" without
        having to read the callee.
        """
        path = self._path_by_qualified.get(target_qualified)
        if not path:
            return CodeRelationshipType.CALLS
        segments = {segment.lower() for segment in path.replace("\\", "/").split("/")}
        stem = posixpath.splitext(posixpath.basename(path))[0].lower()
        if segments & DATA_LAYER_TOKENS or stem in DATA_LAYER_TOKENS:
            return CodeRelationshipType.QUERIES
        return CodeRelationshipType.CALLS

    def _build_modifies_edges(
        self,
        parsed: ParsedFile,
        result: CodeGraphResult,
        seen_edges: set,
        fan_out: dict,
        fan_in: dict,
    ) -> None:
        """Import edges between files (module → module)."""
        source = self._module_symbol.get(parsed.path)
        if not source:
            return
        for reference in parsed.references:
            if reference.kind is not ReferenceKind.IMPORT:
                continue
            target_path = self.module_index.resolve_import(
                parsed.path, reference.module_hint or ""
            )
            target = self._module_symbol.get(target_path) if target_path else None
            if not target or target == source:
                continue
            key = (source, target, CodeRelationshipType.IMPORTS.value, 0)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            result.relationships.append(
                ResolvedRelationship(
                    source_qualified=source,
                    target_qualified=target,
                    relationship_type=CodeRelationshipType.IMPORTS,
                    line=reference.line,
                    confidence=0.8,
                    evidence=f"import of {reference.module_hint}",
                )
            )
            fan_out[source] = fan_out.get(source, 0) + 1
            fan_in[target] = fan_in.get(target, 0) + 1

    def _build_declaration_edges(
        self,
        parsed: ParsedFile,
        result: CodeGraphResult,
        seen_edges: set,
        fan_out: dict,
        fan_in: dict,
    ) -> None:
        """Inheritance, interface implementation, and raise/catch edges."""
        for symbol in parsed.symbols:
            metadata = symbol.metadata or {}
            if symbol.symbol_type.name in ("CLASS", "INTERFACE"):
                for base in metadata.get("bases", []) or []:
                    target, strategy = self._resolve_name(parsed, base.split(".")[-1])
                    if not target:
                        continue
                    key = (
                        symbol.qualified_name,
                        target,
                        CodeRelationshipType.INHERITS.value,
                        symbol.start_line,
                    )
                    if key in seen_edges:
                        continue
                    seen_edges.add(key)
                    result.relationships.append(
                        ResolvedRelationship(
                            source_qualified=symbol.qualified_name,
                            target_qualified=target,
                            relationship_type=CodeRelationshipType.INHERITS,
                            line=symbol.start_line,
                            confidence=CONFIDENCE_BY_RESOLUTION.get(strategy, 0.3),
                            evidence=f"declares base {base}",
                        )
                    )
                    fan_out[symbol.qualified_name] = (
                        fan_out.get(symbol.qualified_name, 0) + 1
                    )
                    fan_in[target] = fan_in.get(target, 0) + 1
            for thrown in metadata.get("raises", []) or []:
                target, strategy = self._resolve_name(parsed, thrown.split(".")[-1])
                if not target:
                    #: A builtin exception is not a node in the snapshot. That is
                    #: not a failure — it is the boundary of static analysis, and
                    #: the raise stays visible on the symbol's metadata.
                    continue
                key = (
                    symbol.qualified_name,
                    target,
                    CodeRelationshipType.THROWS.value,
                    symbol.start_line,
                )
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                result.relationships.append(
                    ResolvedRelationship(
                        source_qualified=symbol.qualified_name,
                        target_qualified=target,
                        relationship_type=CodeRelationshipType.THROWS,
                        line=symbol.start_line,
                        confidence=CONFIDENCE_BY_RESOLUTION.get(strategy, 0.3),
                        evidence=f"raises {thrown}",
                    )
                )
            for caught in metadata.get("catches", []) or []:
                target, strategy = self._resolve_name(parsed, caught.split(".")[-1])
                if not target:
                    continue
                key = (
                    symbol.qualified_name,
                    target,
                    CodeRelationshipType.CATCHES.value,
                    symbol.start_line,
                )
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                result.relationships.append(
                    ResolvedRelationship(
                        source_qualified=symbol.qualified_name,
                        target_qualified=target,
                        relationship_type=CodeRelationshipType.CATCHES,
                        line=symbol.start_line,
                        confidence=CONFIDENCE_BY_RESOLUTION.get(strategy, 0.3),
                        evidence=f"catches {caught}",
                    )
                )

    def _resolve_name(self, parsed: ParsedFile, name: str) -> tuple[Optional[str], str]:
        """Resolve a bare type/exception name to a definition."""
        if not name:
            return None, "unresolved"
        in_file = self._in_file(parsed, name)
        if in_file:
            return in_file, "same-file"
        aliased = self.import_aliases.get((parsed.path, name))
        if aliased:
            return aliased, "import-alias"
        matches = self.by_name.get(name, [])
        if len(matches) == 1:
            return matches[0][0], "unique-name"
        return None, "unresolved"
