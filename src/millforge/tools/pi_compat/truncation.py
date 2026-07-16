"""Pi-derived text truncation primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
    "GREP_MAX_LINE_LENGTH",
    "TruncationResult",
    "format_size",
    "sanitize_text",
    "truncate_head",
    "truncate_line",
    "truncate_tail",
]

DEFAULT_MAX_LINES = 2_000
DEFAULT_MAX_BYTES = 50 * 1024
GREP_MAX_LINE_LENGTH = 500


@dataclass(frozen=True)
class TruncationResult:
    """The Pi-compatible accounting for a text truncation operation."""

    content: str
    truncated: bool
    truncated_by: Literal["lines", "bytes"] | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool
    first_line_exceeds_limit: bool
    max_lines: int
    max_bytes: int


def _utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def sanitize_text(text: str) -> str:
    """Replace lone surrogate code points before text reaches a model result."""

    if not any(0xD800 <= ord(character) <= 0xDFFF for character in text):
        return text
    return "".join(
        "\ufffd" if 0xD800 <= ord(character) <= 0xDFFF else character
        for character in text
    )


def _split_lines_for_counting(content: str) -> list[str]:
    if not content:
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def format_size(byte_count: int) -> str:
    """Format a byte count with the wording used by Pi tool notices."""

    if byte_count < 1024:
        return f"{byte_count}B"
    if byte_count < 1024 * 1024:
        return f"{_round_to_fixed_one(byte_count, 1024)}KB"
    return f"{_round_to_fixed_one(byte_count, 1024 * 1024)}MB"


def _round_to_fixed_one(numerator: int, denominator: int) -> str:
    """Render a non-negative binary-size ratio like JavaScript ``toFixed(1)``."""

    tenths = (numerator * 10 + denominator // 2) // denominator
    return f"{tenths // 10}.{tenths % 10}"


def _unchanged_result(
    content: str,
    *,
    total_lines: int,
    total_bytes: int,
    max_lines: int,
    max_bytes: int,
) -> TruncationResult:
    return TruncationResult(
        content=content,
        truncated=False,
        truncated_by=None,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=total_lines,
        output_bytes=total_bytes,
        last_line_partial=False,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_head(
    content: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """Keep Pi's first complete lines under independent line and byte limits."""

    content = sanitize_text(content)
    total_bytes = _utf8_bytes(content)
    lines = _split_lines_for_counting(content)
    total_lines = len(lines)
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return _unchanged_result(
            content,
            total_lines=total_lines,
            total_bytes=total_bytes,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    if _utf8_bytes(lines[0]) > max_bytes:
        return TruncationResult(
            content="",
            truncated=True,
            truncated_by="bytes",
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=0,
            output_bytes=0,
            last_line_partial=False,
            first_line_exceeds_limit=True,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    output_lines: list[str] = []
    output_bytes = 0
    truncated_by: Literal["lines", "bytes"] = "lines"
    for index, line in enumerate(lines):
        if index >= max_lines:
            break
        line_bytes = _utf8_bytes(line) + (1 if index > 0 else 0)
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        output_lines.append(line)
        output_bytes += line_bytes

    if len(output_lines) >= max_lines and output_bytes <= max_bytes:
        truncated_by = "lines"

    output = "\n".join(output_lines)
    return TruncationResult(
        content=output,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(output_lines),
        output_bytes=_utf8_bytes(output),
        last_line_partial=False,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def _truncate_from_end(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    start = len(data) - max_bytes
    while start < len(data) and data[start] & 0xC0 == 0x80:
        start += 1
    return data[start:].decode("utf-8")


def truncate_tail(
    content: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """Keep Pi's trailing lines, including its single-overlong-line edge case."""

    content = sanitize_text(content)
    total_bytes = _utf8_bytes(content)
    lines = _split_lines_for_counting(content)
    total_lines = len(lines)
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return _unchanged_result(
            content,
            total_lines=total_lines,
            total_bytes=total_bytes,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    output_lines: list[str] = []
    output_bytes = 0
    truncated_by: Literal["lines", "bytes"] = "lines"
    last_line_partial = False
    for line in reversed(lines):
        if len(output_lines) >= max_lines:
            break
        line_bytes = _utf8_bytes(line) + (1 if output_lines else 0)
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            if not output_lines:
                output_lines.insert(0, _truncate_from_end(line, max_bytes))
                last_line_partial = True
            break
        output_lines.insert(0, line)
        output_bytes += line_bytes

    if len(output_lines) >= max_lines and output_bytes <= max_bytes:
        truncated_by = "lines"

    output = "\n".join(output_lines)
    return TruncationResult(
        content=output,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(output_lines),
        output_bytes=_utf8_bytes(output),
        last_line_partial=last_line_partial,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_line(
    line: str, max_characters: int = GREP_MAX_LINE_LENGTH
) -> tuple[str, bool]:
    """Limit one grep line with Pi's model-visible suffix."""

    line = sanitize_text(line)
    if _utf16_code_unit_length(line) <= max_characters:
        return line, False
    return f"{_take_utf16_code_units(line, max_characters)}... [truncated]", True


def _utf16_code_unit_length(text: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in text)


def _take_utf16_code_units(text: str, maximum: int) -> str:
    """Take JS string units without manufacturing a lone Python surrogate."""

    units = 0
    result: list[str] = []
    for character in text:
        character_units = 2 if ord(character) > 0xFFFF else 1
        if units + character_units > maximum:
            break
        result.append(character)
        units += character_units
    return "".join(result)
