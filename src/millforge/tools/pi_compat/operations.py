"""Synchronous filesystem operations in the Pi 0.79.6 compatibility pack."""

from __future__ import annotations

import math
from pathlib import Path

from .contracts import (
    PiCompatErrorKind,
    PiCompatOperationResult,
    PiCompatSideEffectState,
)
from .mutations import file_mutation_lock
from .paths import (
    _PathValidationError,
    _is_absolute_cwd,
    resolve_read_path,
    resolve_to_cwd,
)
from .truncation import DEFAULT_MAX_BYTES, format_size, sanitize_text, truncate_head

__all__ = ["execute_ls", "execute_read", "execute_write"]

_DEFAULT_LS_LIMIT = 500
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _read_only_error(
    message: str, error_kind: PiCompatErrorKind
) -> PiCompatOperationResult:
    return PiCompatOperationResult(
        model_text=sanitize_text(message),
        truncated=False,
        error_kind=error_kind,
        exit_code=None,
        changed_path=None,
        side_effect_state=PiCompatSideEffectState.NOT_ATTEMPTED,
    )


def _invalid_arguments(message: str) -> PiCompatOperationResult:
    return _read_only_error(message, PiCompatErrorKind.INVALID_ARGUMENTS)


def _has_embedded_nul(value: str) -> bool:
    return "\x00" in value


def _cwd_error(cwd: object) -> str | None:
    if not _is_absolute_cwd(cwd):
        return "cwd must be an absolute path"
    if "\x00" in str(cwd):
        return "cwd must not contain NUL bytes"
    return None


def _filesystem_error_kind(error: OSError) -> PiCompatErrorKind:
    if isinstance(error, FileNotFoundError):
        return PiCompatErrorKind.NOT_FOUND
    if isinstance(error, PermissionError):
        return PiCompatErrorKind.PERMISSION_DENIED
    return PiCompatErrorKind.IO_ERROR


def _read_uint32_be(data: bytes, offset: int) -> int:
    if offset + 4 > len(data):
        return 0
    return int.from_bytes(data[offset : offset + 4], "big")


def _is_animated_png(data: bytes) -> bool:
    offset = len(_PNG_SIGNATURE)
    while offset + 8 <= len(data):
        chunk_length = _read_uint32_be(data, offset)
        chunk_type_offset = offset + 4
        if data[chunk_type_offset : chunk_type_offset + 4] == b"acTL":
            return True
        if data[chunk_type_offset : chunk_type_offset + 4] == b"IDAT":
            return False
        next_offset = offset + 8 + chunk_length + 4
        if next_offset <= offset or next_offset > len(data):
            return False
        offset = next_offset
    return False


def _supported_image_mime_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return None if len(data) > 3 and data[3] == 0xF7 else "image/jpeg"
    if data.startswith(_PNG_SIGNATURE):
        is_png = (
            len(data) >= 16
            and _read_uint32_be(data, len(_PNG_SIGNATURE)) == 13
            and data[12:16] == b"IHDR"
        )
        if is_png and not _is_animated_png(data):
            return "image/png"
        return None
    if data.startswith(b"GIF"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number_text(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _js_truthy_number(value: int | float) -> bool:
    return not (value == 0 or (isinstance(value, float) and math.isnan(value)))


def _js_slice_index(value: int | float, length: int) -> int:
    if isinstance(value, float):
        if math.isnan(value):
            return 0
        if value == math.inf:
            return length
        if value == -math.inf:
            return 0
    integer = math.trunc(value)
    if integer < 0:
        return max(length + integer, 0)
    return min(integer, length)


def _read_start_line(
    offset: int | float | None, line_count: int
) -> tuple[int | float, int]:
    if offset is None or not _js_truthy_number(offset):
        start_line: int | float = 0
    else:
        start_line = max(0, offset - 1)
    return start_line, _js_slice_index(start_line, line_count)


def execute_read(
    *,
    cwd: Path,
    path: str,
    offset: int | float | None = None,
    limit: int | float | None = None,
) -> PiCompatOperationResult:
    """Read text or a supported image using Pi's output wording and limits."""

    cwd_error = _cwd_error(cwd)
    if cwd_error is not None:
        return _invalid_arguments(cwd_error)
    if not isinstance(path, str):
        return _invalid_arguments("path must be a string")
    if _has_embedded_nul(path):
        return _invalid_arguments("path must not contain NUL bytes")
    if offset is not None and not _is_number(offset):
        return _invalid_arguments("offset must be a number")
    if limit is not None and not _is_number(limit):
        return _invalid_arguments("limit must be a number")

    try:
        absolute_path = resolve_read_path(path, cwd)
        data = absolute_path.read_bytes()
    except _PathValidationError as error:
        return _invalid_arguments(str(error))
    except OSError as error:
        return _read_only_error(str(error), _filesystem_error_kind(error))
    except UnicodeError:
        return _invalid_arguments("path cannot be encoded by this filesystem")

    mime_type = _supported_image_mime_type(data[:4100])
    if mime_type is not None:
        return PiCompatOperationResult(
            model_text=(
                f"Read image file [{mime_type}]\n"
                "[Image attachment omitted: Millforge's tool-result contract is "
                f"text-only. File size: {len(data)} bytes.]"
            ),
            truncated=False,
            error_kind=None,
            exit_code=None,
            changed_path=None,
            side_effect_state=PiCompatSideEffectState.NOT_ATTEMPTED,
        )

    text_content = data.decode("utf-8", errors="replace")
    all_lines = text_content.split("\n")
    start_line, start_index = _read_start_line(offset, len(all_lines))
    if start_line >= len(all_lines):
        return _invalid_arguments(
            "Offset "
            f"{_number_text(offset if offset is not None else 0)} is beyond end "
            f"of file ({len(all_lines)} lines total)"
        )

    user_limited_lines: int | float | None = None
    if limit is None:
        selected_content = "\n".join(all_lines[start_index:])
    else:
        end_line = min(start_line + limit, len(all_lines))
        end_index = _js_slice_index(end_line, len(all_lines))
        selected_content = "\n".join(all_lines[start_index:end_index])
        user_limited_lines = end_line - start_line

    truncation = truncate_head(selected_content)
    start_line_display = start_line + 1
    if truncation.first_line_exceeds_limit:
        first_line = all_lines[start_index]
        model_text = (
            f"[Line {_number_text(start_line_display)} is "
            f"{format_size(len(first_line.encode('utf-8')))}, exceeds "
            f"{format_size(DEFAULT_MAX_BYTES)} limit. Use bash: sed -n "
            f"'{_number_text(start_line_display)}p' {path} | head -c "
            f"{DEFAULT_MAX_BYTES}]"
        )
    elif truncation.truncated:
        end_line_display = start_line_display + truncation.output_lines - 1
        next_offset = end_line_display + 1
        model_text = truncation.content
        if truncation.truncated_by == "lines":
            model_text += (
                f"\n\n[Showing lines {_number_text(start_line_display)}-"
                f"{_number_text(end_line_display)} of {len(all_lines)}. Use "
                f"offset={_number_text(next_offset)} to continue.]"
            )
        else:
            model_text += (
                f"\n\n[Showing lines {_number_text(start_line_display)}-"
                f"{_number_text(end_line_display)} of {len(all_lines)} "
                f"({format_size(DEFAULT_MAX_BYTES)} limit). Use "
                f"offset={_number_text(next_offset)} to continue.]"
            )
    elif user_limited_lines is not None and start_line + user_limited_lines < len(
        all_lines
    ):
        remaining = len(all_lines) - (start_line + user_limited_lines)
        next_offset = start_line + user_limited_lines + 1
        model_text = (
            f"{truncation.content}\n\n[{_number_text(remaining)} more lines in "
            f"file. Use offset={_number_text(next_offset)} to continue.]"
        )
    else:
        model_text = truncation.content

    return PiCompatOperationResult(
        model_text=sanitize_text(model_text),
        truncated=truncation.truncated,
        error_kind=None,
        exit_code=None,
        changed_path=None,
        side_effect_state=PiCompatSideEffectState.NOT_ATTEMPTED,
    )


def execute_ls(
    *,
    cwd: Path,
    path: str | None = None,
    limit: int | float | None = None,
) -> PiCompatOperationResult:
    """List Pi-compatible directory entries, including dotfiles and directories."""

    cwd_error = _cwd_error(cwd)
    if cwd_error is not None:
        return _invalid_arguments(cwd_error)
    if path is not None and not isinstance(path, str):
        return _invalid_arguments("path must be a string")
    if path is not None and _has_embedded_nul(path):
        return _invalid_arguments("path must not contain NUL bytes")
    if limit is not None and not _is_number(limit):
        return _invalid_arguments("limit must be a number")

    try:
        dir_path = resolve_to_cwd(path or ".", cwd)
        if not dir_path.exists():
            return _read_only_error(
                f"Path not found: {dir_path}", PiCompatErrorKind.NOT_FOUND
            )
        if not dir_path.is_dir():
            return _invalid_arguments(f"Not a directory: {dir_path}")
        entries = list(dir_path.iterdir())
    except _PathValidationError as error:
        return _invalid_arguments(str(error))
    except OSError as error:
        return _read_only_error(
            f"Cannot read directory: {error}", _filesystem_error_kind(error)
        )
    except UnicodeError:
        return _invalid_arguments("path cannot be encoded by this filesystem")

    entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
    effective_limit = _DEFAULT_LS_LIMIT if limit is None else limit
    results: list[str] = []
    entry_limit_reached = False
    for entry in entries:
        if len(results) >= effective_limit:
            entry_limit_reached = True
            break
        try:
            suffix = "/" if entry.is_dir() else ""
        except OSError:
            continue
        results.append(f"{entry.name}{suffix}")

    if not results:
        return PiCompatOperationResult(
            model_text="(empty directory)",
            truncated=False,
            error_kind=None,
            exit_code=None,
            changed_path=None,
            side_effect_state=PiCompatSideEffectState.NOT_ATTEMPTED,
        )

    truncation = truncate_head("\n".join(results), max_lines=2**53 - 1)
    notices: list[str] = []
    if entry_limit_reached:
        notices.append(
            f"{_number_text(effective_limit)} entries limit reached. Use "
            f"limit={_number_text(effective_limit * 2)} for more"
        )
    if truncation.truncated:
        notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
    model_text = truncation.content
    if notices:
        model_text += f"\n\n[{'. '.join(notices)}]"

    return PiCompatOperationResult(
        model_text=sanitize_text(model_text),
        truncated=entry_limit_reached or truncation.truncated,
        error_kind=None,
        exit_code=None,
        changed_path=None,
        side_effect_state=PiCompatSideEffectState.NOT_ATTEMPTED,
    )


def execute_write(
    *,
    cwd: Path,
    path: str,
    content: str,
) -> PiCompatOperationResult:
    """Create parent directories and overwrite one file under the mutation lock."""

    cwd_error = _cwd_error(cwd)
    if cwd_error is not None:
        return _invalid_arguments(cwd_error)
    if not isinstance(path, str):
        return _invalid_arguments("path must be a string")
    if _has_embedded_nul(path):
        return _invalid_arguments("path must not contain NUL bytes")
    if not isinstance(content, str):
        return _invalid_arguments("content must be a string")

    mutation_started = False
    try:
        absolute_path = resolve_to_cwd(path, cwd)
        with file_mutation_lock(absolute_path):
            try:
                mutation_started = True
                absolute_path.parent.mkdir(parents=True, exist_ok=True)
                with absolute_path.open("w", encoding="utf-8", newline="") as output:
                    output.write(content)
            except (OSError, UnicodeError) as error:
                return PiCompatOperationResult(
                    model_text=sanitize_text(str(error)),
                    truncated=False,
                    error_kind=(
                        _filesystem_error_kind(error)
                        if isinstance(error, OSError)
                        else PiCompatErrorKind.IO_ERROR
                    ),
                    exit_code=None,
                    changed_path=None,
                    side_effect_state=(
                        PiCompatSideEffectState.COMPLETION_UNKNOWN
                        if mutation_started
                        else PiCompatSideEffectState.NOT_ATTEMPTED
                    ),
                )
    except _PathValidationError as error:
        return _invalid_arguments(str(error))
    except UnicodeError:
        return _invalid_arguments("path cannot be encoded by this filesystem")

    javascript_string_length = len(content.encode("utf-16-le")) // 2
    return PiCompatOperationResult(
        model_text=sanitize_text(
            f"Successfully wrote {javascript_string_length} bytes to {path}"
        ),
        truncated=False,
        error_kind=None,
        exit_code=None,
        changed_path=absolute_path,
        side_effect_state=PiCompatSideEffectState.CONFIRMED_COMPLETE,
    )
