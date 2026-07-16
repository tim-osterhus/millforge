"""Deterministic Python implementations of Pi's ``grep`` and ``find`` tools.

The public operations intentionally stay independent from Millforge's tool
descriptors.  They are a source-attributed behavioral port of Pi 0.79.6 with
the filesystem-search adaptations specified by Millforge Spec 11 section 7.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import math
import os
from pathlib import Path
import re
import stat
import sys
from typing import Iterator, cast

from pathspec import PathSpec

from .contracts import (
    PiCompatErrorKind,
    PiCompatOperationResult,
    PiCompatSideEffectState,
)
from .paths import _PathValidationError, _is_absolute_cwd, resolve_to_cwd
from .truncation import (
    DEFAULT_MAX_BYTES,
    GREP_MAX_LINE_LENGTH,
    sanitize_text,
    truncate_head,
    truncate_line,
)


_DEFAULT_GREP_LIMIT = 100
_DEFAULT_FIND_LIMIT = 1_000
_MAX_NOTICE_BYTES = 2_048


@dataclass(frozen=True)
class _TraversalEntry:
    path: Path
    relative_path: str
    kind: str


@dataclass(frozen=True)
class _IgnoreSpec:
    base_relative_path: str
    spec: PathSpec


@dataclass(frozen=True)
class _GlobMatcher:
    spec: PathSpec
    matches_path: bool
    matches_nothing: bool = False

    def matches(self, relative_path: str) -> bool:
        if self.matches_nothing:
            return False
        candidate = (
            relative_path if self.matches_path else relative_path.rsplit("/", 1)[-1]
        )
        return self.spec.match_file(candidate)


class _InvalidSearchArgument(ValueError):
    """Raised for a regex or glob that cannot be evaluated by this port."""


class _RootSearchError(OSError):
    """Raised when an explicit search root cannot be accessed."""


class _SearchTraversal:
    """Walk one search root while collecting descendant read failures."""

    def __init__(self) -> None:
        self.unreadable_count = 0
        self._unreadable_paths: set[str] = set()

    def walk(self, root: Path) -> Iterator[_TraversalEntry]:
        yield from self._walk_directory(
            root,
            relative_directory="",
            inherited_ignore_specs=(),
            include_directory=False,
            is_root=True,
        )

    def mark_unreadable(self, path: Path) -> None:
        key = os.fspath(path)
        if key not in self._unreadable_paths:
            self._unreadable_paths.add(key)
            self.unreadable_count += 1

    def was_unreadable(self, path: Path) -> bool:
        return os.fspath(path) in self._unreadable_paths

    def _walk_directory(
        self,
        directory: Path,
        *,
        relative_directory: str,
        inherited_ignore_specs: tuple[_IgnoreSpec, ...],
        include_directory: bool,
        is_root: bool,
    ) -> Iterator[_TraversalEntry]:
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda entry: (entry.name.casefold(), entry.name),
            )
        except OSError as exc:
            if is_root:
                raise _RootSearchError(*exc.args) from exc
            self.mark_unreadable(directory)
            return

        ignore_specs = inherited_ignore_specs
        ignore_file = directory / ".gitignore"
        try:
            ignore_stat = ignore_file.stat()
        except FileNotFoundError:
            ignore_stat = None
        except OSError:
            self.mark_unreadable(ignore_file)
            ignore_stat = None

        if ignore_stat is not None and stat.S_ISREG(ignore_stat.st_mode):
            try:
                ignore_text = ignore_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                self.mark_unreadable(ignore_file)
            else:
                try:
                    ignore_spec = PathSpec.from_lines(
                        "gitwildmatch", ignore_text.splitlines()
                    )
                except (TypeError, ValueError, re.error):
                    # Git ignore files are intentionally forgiving. An invalid
                    # local rule cannot make the search operation itself fail.
                    ignore_spec = None
                if ignore_spec is not None:
                    ignore_specs = (
                        *ignore_specs,
                        _IgnoreSpec(relative_directory, ignore_spec),
                    )

        if include_directory:
            yield _TraversalEntry(directory, relative_directory, "directory")

        for entry in entries:
            child_path = Path(entry.path)
            relative_path = _join_relative_path(relative_directory, entry.name)
            if self.was_unreadable(child_path):
                continue

            try:
                is_symlink = entry.is_symlink()
                is_directory = not is_symlink and entry.is_dir(follow_symlinks=False)
                is_file = not is_symlink and entry.is_file(follow_symlinks=False)
            except OSError:
                self.mark_unreadable(child_path)
                continue

            if _is_ignored(relative_path, is_directory, ignore_specs):
                continue

            if is_symlink:
                yield _TraversalEntry(child_path, relative_path, "symlink")
            elif is_directory:
                yield from self._walk_directory(
                    child_path,
                    relative_directory=relative_path,
                    inherited_ignore_specs=ignore_specs,
                    include_directory=True,
                    is_root=False,
                )
            elif is_file:
                yield _TraversalEntry(child_path, relative_path, "file")


def execute_grep(
    *,
    cwd: Path,
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    ignoreCase: bool | None = None,
    literal: bool | None = None,
    context: int | float | None = None,
    limit: int | float | None = None,
) -> PiCompatOperationResult:
    """Search non-ignored regular files using Pi's model-visible grep format."""

    if not _is_absolute_cwd(cwd):
        return _error_result("cwd must be an absolute path", "invalid_arguments")
    if "\x00" in str(cwd):
        return _error_result("cwd must not contain NUL bytes", "invalid_arguments")
    invalid = _validate_grep_arguments(
        pattern=pattern,
        path=path,
        glob=glob,
        ignore_case=ignoreCase,
        literal=literal,
        context=context,
        limit=limit,
    )
    if invalid is not None:
        return _error_result(invalid, "invalid_arguments")

    try:
        search_root = _resolve_search_path(cwd, path)
        root_kind = _search_root_kind(search_root, find_requires_directory=False)
        matcher = _compile_grep_matcher(pattern, bool(ignoreCase), bool(literal))
        glob_matcher = _compile_glob(glob) if glob else None
    except _PathValidationError as exc:
        return _error_result(str(exc), "invalid_arguments")
    except _InvalidSearchArgument as exc:
        return _error_result(str(exc), "invalid_arguments")
    except OSError as exc:
        return _root_error_result(
            search_root if "search_root" in locals() else cwd, exc
        )
    except UnicodeError:
        return _error_result(
            "path cannot be encoded by this filesystem", "invalid_arguments"
        )

    effective_limit = max(1, limit if limit is not None else _DEFAULT_GREP_LIMIT)
    context_value = context if context is not None and context > 0 else 0
    raw_lines: list[str] = []
    match_count = 0
    match_limit_reached = False
    lines_truncated = False
    traversal = _SearchTraversal()

    def process_file(
        file_path: Path, display_path: str, *, explicit_root: bool = False
    ) -> None:
        nonlocal lines_truncated, match_count, match_limit_reached
        if glob_matcher is not None and not glob_matcher.matches(display_path):
            return
        try:
            data = _read_search_file(file_path)
        except OSError as exc:
            if explicit_root:
                raise _RootSearchError(*exc.args) from exc
            traversal.mark_unreadable(file_path)
            return
        if data is None:
            return

        matching_lines, context_source_lines = _split_grep_lines(
            data, context_value > 0
        )
        for line_number, line in enumerate(matching_lines, start=1):
            if not matcher.search(line):
                continue
            if match_count >= effective_limit:
                match_limit_reached = True
                return

            match_count += 1
            if context_value == 0:
                rendered, was_truncated = truncate_line(line)
                lines_truncated = lines_truncated or was_truncated
                raw_lines.append(f"{display_path}:{line_number}: {rendered}")
            else:
                for current_line_number in _context_line_numbers(
                    line_number, len(context_source_lines), context_value
                ):
                    source_line = _context_source_line(
                        context_source_lines, current_line_number
                    )
                    rendered, was_truncated = truncate_line(source_line)
                    lines_truncated = lines_truncated or was_truncated
                    if current_line_number == line_number:
                        raw_lines.append(
                            f"{display_path}:{_format_number(current_line_number)}: "
                            f"{rendered}"
                        )
                    else:
                        raw_lines.append(
                            f"{display_path}-{_format_number(current_line_number)}- "
                            f"{rendered}"
                        )

            if match_count >= effective_limit:
                match_limit_reached = True
                return

    if root_kind == "file":
        try:
            process_file(search_root, search_root.name, explicit_root=True)
        except _RootSearchError as exc:
            return _root_error_result(search_root, exc)
    else:
        try:
            for entry in traversal.walk(search_root):
                if match_limit_reached:
                    break
                if entry.kind != "file":
                    continue
                process_file(entry.path, entry.relative_path)
        except _RootSearchError as exc:
            return _root_error_result(search_root, exc)

    rendered_output, byte_truncated = _render_search_output(
        raw_lines,
        empty_text="No matches found",
        match_or_result_limit_reached=match_limit_reached,
        match_or_result_notice=(
            f"{_format_number(effective_limit)} matches limit reached. "
            f"Use limit={_format_number(effective_limit * 2)} for more, or refine pattern"
        ),
        long_line_notice=(
            f"Some lines truncated to {GREP_MAX_LINE_LENGTH} chars. Use read tool to see full lines"
            if lines_truncated
            else None
        ),
        unreadable_count=traversal.unreadable_count,
    )
    return _success_result(
        rendered_output,
        truncated=(
            match_limit_reached
            or byte_truncated
            or lines_truncated
            or traversal.unreadable_count > 0
        ),
    )


def execute_find(
    *,
    cwd: Path,
    pattern: str,
    path: str | None = None,
    limit: int | float | None = None,
) -> PiCompatOperationResult:
    """Find non-ignored paths using Pi's relative-path result format."""

    if not _is_absolute_cwd(cwd):
        return _error_result("cwd must be an absolute path", "invalid_arguments")
    if "\x00" in str(cwd):
        return _error_result("cwd must not contain NUL bytes", "invalid_arguments")
    invalid = _validate_find_arguments(pattern=pattern, path=path, limit=limit)
    if invalid is not None:
        return _error_result(invalid, "invalid_arguments")

    try:
        search_root = _resolve_search_path(cwd, path)
        _search_root_kind(search_root, find_requires_directory=True)
        matcher = _compile_glob(pattern)
    except _PathValidationError as exc:
        return _error_result(str(exc), "invalid_arguments")
    except _InvalidSearchArgument as exc:
        return _error_result(str(exc), "invalid_arguments")
    except OSError as exc:
        return _root_error_result(
            search_root if "search_root" in locals() else cwd, exc
        )
    except UnicodeError:
        return _error_result(
            "path cannot be encoded by this filesystem", "invalid_arguments"
        )

    effective_limit = limit if limit is not None else _DEFAULT_FIND_LIMIT
    raw_lines: list[str] = []
    result_limit_reached = False
    traversal = _SearchTraversal()

    try:
        for entry in traversal.walk(search_root):
            if result_limit_reached:
                break
            if not matcher.matches(entry.relative_path):
                continue

            if entry.kind == "file" and not _can_open_for_read(entry.path):
                traversal.mark_unreadable(entry.path)
                continue

            raw_lines.append(
                f"{entry.relative_path}/"
                if entry.kind == "directory"
                else entry.relative_path
            )
            if effective_limit != 0 and len(raw_lines) >= effective_limit:
                result_limit_reached = True
    except _RootSearchError as exc:
        return _root_error_result(search_root, exc)

    if effective_limit == 0 and raw_lines:
        result_limit_reached = True

    rendered_output, byte_truncated = _render_search_output(
        raw_lines,
        empty_text="No files found matching pattern",
        match_or_result_limit_reached=result_limit_reached,
        match_or_result_notice=(
            f"{_format_number(effective_limit)} results limit reached. "
            f"Use limit={_format_number(effective_limit * 2)} for more, or refine pattern"
        ),
        long_line_notice=None,
        unreadable_count=traversal.unreadable_count,
    )
    return _success_result(
        rendered_output,
        truncated=result_limit_reached
        or byte_truncated
        or traversal.unreadable_count > 0,
    )


def _validate_grep_arguments(
    *,
    pattern: object,
    path: object,
    glob: object,
    ignore_case: object,
    literal: object,
    context: object,
    limit: object,
) -> str | None:
    if not isinstance(pattern, str):
        return "grep pattern must be a string"
    if path is not None and not isinstance(path, str):
        return "grep path must be a string"
    if isinstance(path, str) and "\x00" in path:
        return "grep path must not contain NUL bytes"
    if glob is not None and not isinstance(glob, str):
        return "grep glob must be a string"
    if ignore_case is not None and not isinstance(ignore_case, bool):
        return "grep ignoreCase must be a boolean"
    if literal is not None and not isinstance(literal, bool):
        return "grep literal must be a boolean"
    if not _is_json_number_or_none(context):
        return "grep context must be a number"
    if not _is_json_number_or_none(limit):
        return "grep limit must be a number"
    return None


def _validate_find_arguments(
    *, pattern: object, path: object, limit: object
) -> str | None:
    if not isinstance(pattern, str):
        return "find pattern must be a string"
    if path is not None and not isinstance(path, str):
        return "find path must be a string"
    if isinstance(path, str) and "\x00" in path:
        return "find path must not contain NUL bytes"
    if not _is_json_number_or_none(limit):
        return "find limit must be a number"
    numeric_limit = cast(int | float | None, limit)
    if numeric_limit is not None and (
        numeric_limit < 0
        or (isinstance(numeric_limit, float) and not numeric_limit.is_integer())
        or numeric_limit > sys.maxsize
    ):
        return "find limit must be a non-negative integer"
    return None


def _is_json_number_or_none(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _resolve_search_path(cwd: Path, supplied_path: str | None) -> Path:
    if not cwd.is_absolute():
        raise _InvalidSearchArgument("cwd must be an absolute path")
    return resolve_to_cwd(supplied_path or ".", cwd)


def _search_root_kind(path: Path, *, find_requires_directory: bool) -> str:
    try:
        path_stat = path.stat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(*exc.args) from exc

    if stat.S_ISDIR(path_stat.st_mode):
        return "directory"
    if stat.S_ISREG(path_stat.st_mode) and not find_requires_directory:
        return "file"
    if find_requires_directory:
        raise _InvalidSearchArgument(f"Find path must be a directory: {path}")
    raise _InvalidSearchArgument(f"Grep path must be a file or directory: {path}")


def _compile_grep_matcher(
    pattern: str, ignore_case: bool, literal: bool
) -> re.Pattern[str]:
    source = re.escape(pattern) if literal else pattern
    try:
        return re.compile(source, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise _InvalidSearchArgument(f"Invalid regex pattern: {exc}") from exc


def _compile_glob(pattern: str) -> _GlobMatcher:
    _validate_glob_character_classes(pattern)
    matches_path = "/" in pattern
    if pattern.endswith("/"):
        return _GlobMatcher(
            PathSpec.from_lines("gitwildmatch", ()),
            matches_path,
            matches_nothing=True,
        )
    pattern_for_match = pattern or "*"
    if pattern_for_match.startswith("!"):
        pattern_for_match = f"\\{pattern_for_match}"
    effective_pattern = pattern_for_match
    if (
        matches_path
        and not pattern_for_match.startswith(("/", "**/"))
        and pattern_for_match != "**"
    ):
        effective_pattern = f"**/{pattern_for_match}"
    try:
        spec = PathSpec.from_lines("gitwildmatch", (effective_pattern,))
    except (TypeError, ValueError, re.error) as exc:
        raise _InvalidSearchArgument(f"Invalid glob pattern: {pattern}") from exc
    patterns = tuple(spec.patterns)
    if not patterns:
        raise _InvalidSearchArgument(f"Invalid glob pattern: {pattern}")
    return _GlobMatcher(spec, matches_path)


def _validate_glob_character_classes(pattern: str) -> None:
    escaped = False
    in_character_class = False
    for character in pattern:
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
        elif character == "[":
            in_character_class = True
        elif character == "]" and in_character_class:
            in_character_class = False
    if in_character_class:
        raise _InvalidSearchArgument(f"Invalid glob pattern: {pattern}")


def _is_ignored(
    relative_path: str,
    is_directory: bool,
    ignore_specs: tuple[_IgnoreSpec, ...],
) -> bool:
    ignored = False
    for ignore_spec in ignore_specs:
        candidate = _relative_to_ignore_base(
            relative_path, ignore_spec.base_relative_path
        )
        if candidate is None:
            continue
        if is_directory:
            candidate = f"{candidate}/"

        decision: bool | None = None
        for pattern in ignore_spec.spec.patterns:
            if pattern.include is not None and pattern.match_file(candidate):
                decision = bool(pattern.include)
        if decision is not None:
            ignored = decision
    return ignored


def _relative_to_ignore_base(relative_path: str, base_relative_path: str) -> str | None:
    if not base_relative_path:
        return relative_path
    prefix = f"{base_relative_path}/"
    if relative_path.startswith(prefix):
        return relative_path[len(prefix) :]
    return None


def _join_relative_path(parent: str, child: str) -> str:
    return f"{parent}/{child}" if parent else child


def _read_search_file(path: Path) -> bytes | None:
    with path.open("rb") as handle:
        first_bytes = handle.read(8 * 1024)
        if b"\x00" in first_bytes:
            return None
        return first_bytes + handle.read()


def _can_open_for_read(path: Path) -> bool:
    try:
        with path.open("rb"):
            return True
    except OSError:
        return False


def _split_grep_lines(
    data: bytes, include_context_lines: bool
) -> tuple[list[str], list[str]]:
    text = data.decode("utf-8", errors="replace")
    matching_text = text.replace("\r\n", "\n").replace("\r", "")
    matching_lines = _split_without_terminal_empty_line(matching_text)

    if not include_context_lines:
        return matching_lines, matching_lines

    context_text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not context_text:
        return matching_lines, []
    return matching_lines, context_text.split("\n")


def _split_without_terminal_empty_line(text: str) -> list[str]:
    if not text:
        return []
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return lines


def _context_line_numbers(
    line_number: int, line_count: int, context: int | float
) -> Iterator[int | float]:
    """Yield the same sequence as Pi's JavaScript context loop."""

    start = max(1, line_number - context)
    end = min(line_count, line_number + context)
    if isinstance(context, float) and not context.is_integer():
        current: int | float = start
        while current <= end:
            yield current
            current += 1
        return
    yield from range(int(start), int(end) + 1)


def _context_source_line(lines: list[str], line_number: int | float) -> str:
    if isinstance(line_number, float) and not line_number.is_integer():
        return ""
    index = int(line_number) - 1
    return lines[index] if 0 <= index < len(lines) else ""


def _render_search_output(
    raw_lines: list[str],
    *,
    empty_text: str,
    match_or_result_limit_reached: bool,
    match_or_result_notice: str,
    long_line_notice: str | None,
    unreadable_count: int,
) -> tuple[str, bool]:
    raw_output = "\n".join(raw_lines)
    if raw_lines:
        output, byte_truncated = _truncate_head_to_bytes(raw_output)
    else:
        output = empty_text
        byte_truncated = False

    notices: list[str] = []
    if match_or_result_limit_reached:
        notices.append(match_or_result_notice)
    if byte_truncated:
        notices.append("50.0KB limit reached")
    if long_line_notice is not None:
        notices.append(long_line_notice)
    if unreadable_count:
        notices.append(f"Skipped {unreadable_count} unreadable path(s)")
    if notices:
        notice_text = _truncate_utf8(". ".join(notices), _MAX_NOTICE_BYTES)
        output += f"\n\n[{notice_text}]"
    return output, byte_truncated


def _truncate_head_to_bytes(content: str) -> tuple[str, bool]:
    truncation = truncate_head(
        content, max_lines=2**53 - 1, max_bytes=DEFAULT_MAX_BYTES
    )
    return truncation.content, truncation.truncated


def _truncate_utf8(text: str, max_bytes: int) -> str:
    text = sanitize_text(text)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _format_number(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _success_result(model_text: str, *, truncated: bool) -> PiCompatOperationResult:
    return PiCompatOperationResult(
        model_text=sanitize_text(model_text),
        truncated=truncated,
        error_kind=None,
        exit_code=None,
        changed_path=None,
        side_effect_state=PiCompatSideEffectState("not_attempted"),
    )


def _error_result(model_text: str, error_kind: str) -> PiCompatOperationResult:
    return PiCompatOperationResult(
        model_text=sanitize_text(model_text),
        truncated=False,
        error_kind=PiCompatErrorKind(error_kind),
        exit_code=None,
        changed_path=None,
        side_effect_state=PiCompatSideEffectState("not_attempted"),
    )


def _root_error_result(path: Path, exc: OSError) -> PiCompatOperationResult:
    if isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT:
        return _error_result(f"Path not found: {path}", "not_found")
    if isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EPERM}:
        return _error_result(f"Permission denied: {path}", "permission_denied")
    return _error_result(f"Unable to access path: {path}", "io_error")
