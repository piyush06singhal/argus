"""ARGUS Patch Parser (Phase 7 §11, §12).

Parses **unified diff** text into a structured, validated form before anything
may touch a repository. The parser is the first gate: a patch that cannot be
parsed is never applied, never stored as applicable, and never shown as a
candidate — it is recorded as ``PARSE_FAILED`` with the reason.

What this module guarantees:

* **Standard format only.** Headers ``--- a/path`` / ``+++ b/path``, ``@@ -l,c
  +l,c @@`` hunks, and `` ``/``+``/``-`` lines. Anything else is rejected with
  a precise reason rather than guessed at.
* **Path traversal is impossible by construction** (§12). Every path is
  normalized; absolute paths, ``..`` segments, symlink-escape attempts via
  ``..\\`` and paths that escape the workspace root are all parse errors — not
  runtime discoveries.
* **Counts are measured.** Files, lines added/removed are computed from the
  hunks themselves so the minimal-patch analysis (§9) cannot be flattered by
  whatever the generator claimed.

What this module never does: apply anything, resolve whether a path is inside
a *particular* fix's scope (that is the safety validator's job with the
hypothesis's allowlist), or interpret the content semantically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional


class PatchParseError(ValueError):
    """A patch could not be parsed, with the reason an engineer can act on."""

    def __init__(self, reason: str, line_no: Optional[int] = None) -> None:
        self.reason = reason
        self.line_no = line_no
        location = f" (line {line_no})" if line_no else ""
        super().__init__(f"{reason}{location}")


@dataclass
class HunkLine:
    """One line of a hunk: context, addition, or removal."""

    kind: str  # ' ', '+', '-'
    content: str

    @property
    def is_addition(self) -> bool:
        return self.kind == "+"

    @property
    def is_removal(self) -> bool:
        return self.kind == "-"


@dataclass
class Hunk:
    """One ``@@`` hunk with its declared and actual line ranges."""

    source_start: int
    source_count: int
    target_start: int
    target_count: int
    lines: List[HunkLine] = field(default_factory=list)

    def validate_counts(self) -> None:
        actual_source = sum(1 for line in self.lines if not line.is_addition)
        actual_target = sum(1 for line in self.lines if not line.is_removal)
        if actual_source != self.source_count:
            raise PatchParseError(
                f"hunk declares {self.source_count} source lines but contains "
                f"{actual_source}"
            )
        if actual_target != self.target_count:
            raise PatchParseError(
                f"hunk declares {self.target_count} target lines but contains "
                f"{actual_target}"
            )


@dataclass
class FilePatch:
    """One file's portion of a patch."""

    source_path: str
    target_path: str
    hunks: List[Hunk] = field(default_factory=list)
    #: Mode changes a unified diff can carry (``new file mode``, ``deleted``).
    new_file: bool = False
    deleted_file: bool = False
    binary: bool = False

    @property
    def path(self) -> str:
        """The path the patch leaves the file at."""
        return self.target_path or self.source_path

    @property
    def lines_added(self) -> int:
        return sum(1 for hunk in self.hunks for line in hunk.lines if line.is_addition)

    @property
    def lines_removed(self) -> int:
        return sum(1 for hunk in self.hunks for line in hunk.lines if line.is_removal)


@dataclass
class ParsedPatch:
    """A fully parsed patch: files, measured counts, nothing inferred."""

    files: List[FilePatch] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def lines_added(self) -> int:
        return sum(item.lines_added for item in self.files)

    @property
    def lines_removed(self) -> int:
        return sum(item.lines_removed for item in self.files)

    @property
    def paths(self) -> List[str]:
        """Every path the patch touches, in first-appearance order."""
        seen: dict[str, None] = {}
        for item in self.files:
            seen.setdefault(item.source_path, None)
            if item.target_path and item.target_path != item.source_path:
                seen.setdefault(item.target_path, None)
        return list(seen)

    def has_binary(self) -> bool:
        return any(item.binary for item in self.files)

    def has_deletion(self) -> bool:
        return any(item.deleted_file for item in self.files)

    def to_unified_diff(self) -> str:
        """Render back to canonical unified-diff text.

        Rendering from the parsed structure (rather than echoing the input)
        is what makes "apply exactly what was validated" true: the text the
        applier receives is byte-for-byte what this parser understood.
        """
        out: List[str] = []
        for item in self.files:
            if item.new_file:
                out.append("--- /dev/null")
            else:
                out.append(f"--- a/{item.source_path}")
            if item.deleted_file:
                out.append("+++ /dev/null")
            else:
                out.append(f"+++ b/{item.target_path}")
            for hunk in item.hunks:
                out.append(
                    f"@@ -{hunk.source_start},{hunk.source_count} "
                    f"+{hunk.target_start},{hunk.target_count} @@"
                )
                for line in hunk.lines:
                    out.append(f"{line.kind}{line.content}")
        return "\n".join(out) + "\n"


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

#: Prefixes a diff may use for file headers. ``a/`` and ``b/`` are stripped;
#: ``/dev/null`` is legal for new and deleted files.
_HEADER_TARGETS = ("a/", "b/")


def normalise_path(raw: str, *, line_no: Optional[int] = None) -> str:
    """Normalise and safety-check one patch path (§12).

    Rejects, in this order: absolute paths, Windows drives and back-traversal
    (``..\\``), any ``..`` segment, and — after normalisation — anything that
    is empty or still tries to leave the workspace. The result is always a
    clean relative path with forward slashes.
    """
    if not raw or not raw.strip():
        raise PatchParseError("empty file path", line_no)
    candidate = raw.strip()
    if candidate in {"/dev/null", "a/dev/null", "b/dev/null"}:
        # Handled by the caller for new/deleted files; a path *equal* to
        # /dev/null as a real target is not meaningful here.
        raise PatchParseError(
            "'/dev/null' is only valid as a header placeholder", line_no
        )
    if candidate.startswith("/"):
        raise PatchParseError(
            f"absolute path {candidate!r} is not allowed inside a patch", line_no
        )
    if re.match(r"^[A-Za-z]:", candidate) or "\\" in candidate:
        raise PatchParseError(
            f"windows-style path {candidate!r} is not allowed inside a patch", line_no
        )
    for prefix in _HEADER_TARGETS:
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    if candidate.startswith("/"):
        raise PatchParseError(
            f"absolute path {candidate!r} is not allowed inside a patch", line_no
        )
    # POSIX-normalise without touching the filesystem.
    parts: List[str] = []
    for segment in candidate.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise PatchParseError(
                f"path {raw!r} contains '..' — path traversal is rejected", line_no
            )
        parts.append(segment)
    if not parts:
        raise PatchParseError(f"path {raw!r} normalises to nothing", line_no)
    normalised = "/".join(parts)
    if normalised.startswith("./"):
        raise PatchParseError(f"path {normalised!r} escapes the workspace", line_no)
    return normalised


def _parse_header_path(
    raw: str, *, line_no: int, allow_devnull: bool = False
) -> Optional[str]:
    """Parse one side of a ``---``/``+++`` header."""
    candidate = raw.strip()
    # A timestamp after a tab is legal in unified diffs; drop it.
    candidate = candidate.split("\t", 1)[0].strip()
    if candidate == "/dev/null":
        if allow_devnull:
            return None
        raise PatchParseError("unexpected /dev/null header", line_no)
    if not candidate:
        raise PatchParseError("empty file header path", line_no)
    return normalise_path(candidate, line_no=line_no)


def parse_unified_diff(text: str) -> ParsedPatch:
    """Parse unified-diff text into a :class:`ParsedPatch` (§11).

    Raises :class:`PatchParseError` with a line number for every rejection
    class the execution prompt names: malformed headers, malformed hunks,
    counts that disagree with content, and traversal attempts.
    """
    if not text or not text.strip():
        raise PatchParseError("empty patch")

    lines = text.replace("\r\n", "\n").split("\n")
    parsed = ParsedPatch()
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if not line.startswith(("--- ", "diff ", "Index: ")):
            raise PatchParseError(
                f"expected a '--- ' file header, found: {line[:60]!r}", index + 1
            )
        if line.startswith(("diff ", "Index: ")):
            # Git-style preamble; skip to the --- header.
            index += 1
            while index < total and not lines[index].startswith("--- "):
                if lines[index].startswith("@@"):
                    break
                index += 1
            continue

        # ---- old header ---------------------------------------------------
        old_path = _parse_header_path(line[4:], line_no=index + 1, allow_devnull=True)
        index += 1
        if index >= total or not lines[index].startswith("+++ "):
            raise PatchParseError(
                "missing '+++ ' header after '--- ' header", index + 1
            )
        new_path = _parse_header_path(
            lines[index][4:], line_no=index + 1, allow_devnull=True
        )
        index += 1

        file_patch = FilePatch(
            source_path=old_path or "",
            target_path=new_path or "",
        )
        if old_path is None and new_path is not None:
            file_patch.new_file = True
            file_patch.source_path = new_path
        if new_path is None and old_path is not None:
            file_patch.deleted_file = True
            file_patch.target_path = old_path
        if old_path is None and new_path is None:
            raise PatchParseError("both sides of the header are /dev/null", index)
        if (
            not file_patch.new_file
            and not file_patch.deleted_file
            and file_patch.source_path != file_patch.target_path
        ):
            # A rename touches both paths; allowed, but recorded as such.
            pass

        # Git binary marker (if a preamble said so) — hunks are not expected.
        # ---- hunks --------------------------------------------------------
        saw_binary_note = False
        while index < total:
            current = lines[index]
            if current.startswith("@@ "):
                match = _HUNK_RE.match(current)
                if not match:
                    raise PatchParseError(
                        f"malformed hunk header: {current[:60]!r}", index + 1
                    )
                source_start = int(match.group(1))
                source_count = int(match.group(2) or "1")
                target_start = int(match.group(3))
                target_count = int(match.group(4) or "1")
                hunk = Hunk(
                    source_start=source_start,
                    source_count=source_count,
                    target_start=target_start,
                    target_count=target_count,
                )
                index += 1
                #: A context line consumes one line from *each* side; a body
                #: line consumes one from its own side only. The hunk is
                #: complete only when BOTH declared counts are satisfied —
                #: bounding by max() would stop one line early whenever the
                #: change is not purely additive or purely subtractive.
                source_seen = 0
                target_seen = 0
                while index < total and (
                    source_seen < source_count or target_seen < target_count
                ):
                    body = lines[index]
                    if body.startswith("\\"):
                        # "\ No newline at end of file" — part of the hunk.
                        index += 1
                        continue
                    if not body:
                        # An empty line inside a hunk is a context line of "".
                        hunk.lines.append(HunkLine(kind=" ", content=""))
                        source_seen += 1
                        target_seen += 1
                        index += 1
                        continue
                    if body[0] == " ":
                        hunk.lines.append(HunkLine(kind=" ", content=body[1:]))
                        source_seen += 1
                        target_seen += 1
                        index += 1
                        continue
                    if body[0] == "+":
                        hunk.lines.append(HunkLine(kind="+", content=body[1:]))
                        target_seen += 1
                        index += 1
                        continue
                    if body[0] == "-":
                        hunk.lines.append(HunkLine(kind="-", content=body[1:]))
                        source_seen += 1
                        index += 1
                        continue
                    raise PatchParseError(
                        f"unexpected line inside hunk: {body[:60]!r}", index + 1
                    )
                try:
                    hunk.validate_counts()
                except PatchParseError as error:
                    raise PatchParseError(error.reason, index) from error
                file_patch.hunks.append(hunk)
                continue
            if current.startswith(("diff ", "Index: ", "--- ")):
                break
            if current.startswith("GIT binary patch") or "Binary files" in current:
                saw_binary_note = True
                index += 1
                break
            if not current.strip():
                index += 1
                break
            raise PatchParseError(
                f"unexpected content after hunk: {current[:60]!r}", index + 1
            )

        if saw_binary_note and not file_patch.hunks:
            file_patch.binary = True
        elif not file_patch.hunks and not (
            file_patch.new_file or file_patch.deleted_file
        ):
            raise PatchParseError(
                f"file {file_patch.path!r} has a header but no hunks", index
            )
        #: A deletion expressed as ``@@ -1,N +0,0 @@`` with only removals (and
        #: no ``/dev/null`` header) is still a whole-file deletion — the §55
        #: test-deletion check must see it as one.
        if (
            not file_patch.new_file
            and file_patch.hunks
            and all(
                hunk.target_count == 0
                and hunk.source_count > 0
                and all(line.is_removal for line in hunk.lines if line.kind != " ")
                for hunk in file_patch.hunks
            )
        ):
            file_patch.deleted_file = True
            file_patch.target_path = file_patch.source_path
        parsed.files.append(file_patch)

    if not parsed.files:
        raise PatchParseError("patch contains no file sections")
    return parsed


__all__ = [
    "FilePatch",
    "Hunk",
    "HunkLine",
    "PatchParseError",
    "ParsedPatch",
    "normalise_path",
    "parse_unified_diff",
]
