"""Pi-derived exact and fuzzy multi-edit behavior."""

from __future__ import annotations

import errno
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .contracts import (
    PiCompatErrorKind,
    PiCompatOperationResult,
    PiCompatSideEffectState,
)
from .mutations import file_mutation_lock
from .paths import _PathValidationError, _is_absolute_cwd, resolve_to_cwd
from .truncation import sanitize_text

__all__ = [
    "Edit",
    "PiCompatEditError",
    "apply_edits_to_normalized_content",
    "detect_line_ending",
    "execute_edit",
    "normalize_for_fuzzy_match",
    "normalize_to_lf",
    "restore_line_endings",
    "strip_bom",
]


@dataclass(frozen=True)
class Edit:
    """One Pi edit replacement."""

    old_text: str
    new_text: str


@dataclass(frozen=True)
class _MatchedEdit:
    edit_index: int
    match_index: int
    match_length: int
    new_text: str


@dataclass(frozen=True)
class _Match:
    found: bool
    index: int
    length: int
    used_fuzzy_match: bool


class PiCompatEditError(ValueError):
    """A Pi-compatible edit validation conflict."""


_FUZZY_TRANSLATION_TABLE = str.maketrans(
    cast(
        dict[str, str | int | None],
        {
            "\u2010": "-",
            "\u2011": "-",
            "\u2012": "-",
            "\u2013": "-",
            "\u2014": "-",
            "\u2015": "-",
            "\u2212": "-",
            "\u00a0": " ",
            "\u2002": " ",
            "\u2003": " ",
            "\u2004": " ",
            "\u2005": " ",
            "\u2006": " ",
            "\u2007": " ",
            "\u2008": " ",
            "\u2009": " ",
            "\u200a": " ",
            "\u202f": " ",
            "\u205f": " ",
            "\u3000": " ",
        },
    )
)


def detect_line_ending(content: str) -> str:
    """Return Pi's detected output line ending."""

    crlf_index = content.find("\r\n")
    lf_index = content.find("\n")
    if lf_index == -1 or crlf_index == -1:
        return "\n"
    return "\r\n" if crlf_index < lf_index else "\n"


def normalize_to_lf(text: str) -> str:
    """Normalize all supported newline spellings to LF."""

    return text.replace("\r\n", "\n").replace("\r", "\n")


def restore_line_endings(text: str, ending: str) -> str:
    """Restore Pi's detected CRLF convention after applying edits."""

    return text.replace("\n", "\r\n") if ending == "\r\n" else text


def strip_bom(content: str) -> tuple[str, str]:
    """Split a UTF-8 BOM from the text that the model matches."""

    if content.startswith("\ufeff"):
        return "\ufeff", content[1:]
    return "", content


def normalize_for_fuzzy_match(text: str) -> str:
    """Apply Pi's progressive Unicode and trailing-whitespace normalization."""

    normalized = unicodedata.normalize("NFKC", text)
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    return (
        normalized.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201a", "'")
        .replace("\u201b", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u201e", '"')
        .replace("\u201f", '"')
        .translate(_FUZZY_TRANSLATION_TABLE)
    )


def _fuzzy_find_text(content: str, old_text: str) -> _Match:
    exact_index = content.find(old_text)
    if exact_index != -1:
        return _Match(True, exact_index, len(old_text), False)

    fuzzy_content = normalize_for_fuzzy_match(content)
    fuzzy_old_text = normalize_for_fuzzy_match(old_text)
    fuzzy_index = fuzzy_content.find(fuzzy_old_text)
    if fuzzy_index == -1:
        return _Match(False, -1, 0, False)
    return _Match(True, fuzzy_index, len(fuzzy_old_text), True)


def _count_occurrences(content: str, old_text: str) -> int:
    return normalize_for_fuzzy_match(content).count(normalize_for_fuzzy_match(old_text))


def _not_found_error(path: str, edit_index: int, total_edits: int) -> PiCompatEditError:
    if total_edits == 1:
        return PiCompatEditError(
            f"Could not find the exact text in {path}. The old text must match "
            "exactly including all whitespace and newlines."
        )
    return PiCompatEditError(
        f"Could not find edits[{edit_index}] in {path}. The oldText must match "
        "exactly including all whitespace and newlines."
    )


def _duplicate_error(
    path: str, edit_index: int, total_edits: int, occurrences: int
) -> PiCompatEditError:
    if total_edits == 1:
        return PiCompatEditError(
            f"Found {occurrences} occurrences of the text in {path}. The text "
            "must be unique. Please provide more context to make it unique."
        )
    return PiCompatEditError(
        f"Found {occurrences} occurrences of edits[{edit_index}] in {path}. "
        "Each oldText must be unique. Please provide more context to make it unique."
    )


def _empty_old_text_error(
    path: str, edit_index: int, total_edits: int
) -> PiCompatEditError:
    if total_edits == 1:
        return PiCompatEditError(f"oldText must not be empty in {path}.")
    return PiCompatEditError(
        f"edits[{edit_index}].oldText must not be empty in {path}."
    )


def _no_change_error(path: str, total_edits: int) -> PiCompatEditError:
    if total_edits == 1:
        return PiCompatEditError(
            f"No changes made to {path}. The replacement produced identical content. "
            "This might indicate an issue with special characters or the text not "
            "existing as expected."
        )
    return PiCompatEditError(
        f"No changes made to {path}. The replacements produced identical content."
    )


def apply_edits_to_normalized_content(
    normalized_content: str, edits: Sequence[Edit], path: str
) -> tuple[str, str]:
    """Match all Pi edits against one original LF-normalized content snapshot."""

    normalized_edits = [
        Edit(normalize_to_lf(edit.old_text), normalize_to_lf(edit.new_text))
        for edit in edits
    ]
    for index, edit in enumerate(normalized_edits):
        if not edit.old_text:
            raise _empty_old_text_error(path, index, len(normalized_edits))

    initial_matches = [
        _fuzzy_find_text(normalized_content, edit.old_text) for edit in normalized_edits
    ]
    base_content = (
        normalize_for_fuzzy_match(normalized_content)
        if any(match.used_fuzzy_match for match in initial_matches)
        else normalized_content
    )

    matched_edits: list[_MatchedEdit] = []
    for index, edit in enumerate(normalized_edits):
        match = _fuzzy_find_text(base_content, edit.old_text)
        if not match.found:
            raise _not_found_error(path, index, len(normalized_edits))
        occurrences = _count_occurrences(base_content, edit.old_text)
        if occurrences > 1:
            raise _duplicate_error(path, index, len(normalized_edits), occurrences)
        matched_edits.append(
            _MatchedEdit(index, match.index, match.length, edit.new_text)
        )

    matched_edits.sort(key=lambda edit: edit.match_index)
    for previous, current in zip(matched_edits, matched_edits[1:], strict=False):
        if previous.match_index + previous.match_length > current.match_index:
            raise PiCompatEditError(
                f"edits[{previous.edit_index}] and edits[{current.edit_index}] "
                f"overlap in {path}. Merge them into one edit or target disjoint regions."
            )

    new_content = base_content
    for matched_edit in reversed(matched_edits):
        new_content = (
            new_content[: matched_edit.match_index]
            + matched_edit.new_text
            + new_content[matched_edit.match_index + matched_edit.match_length :]
        )
    if base_content == new_content:
        raise _no_change_error(path, len(normalized_edits))
    return base_content, new_content


def _invalid_result(message: str) -> PiCompatOperationResult:
    return PiCompatOperationResult(
        model_text=sanitize_text(message),
        truncated=False,
        error_kind=PiCompatErrorKind.INVALID_ARGUMENTS,
        exit_code=None,
        changed_path=None,
        side_effect_state=PiCompatSideEffectState.NOT_ATTEMPTED,
    )


def _error_code(error: OSError | UnicodeError) -> str:
    if isinstance(error, OSError) and error.errno == errno.ENOENT:
        return "ENOENT"
    if isinstance(error, OSError) and error.errno in {errno.EACCES, errno.EPERM}:
        return "EACCES"
    return type(error).__name__


def _filesystem_error_result(
    error: OSError | UnicodeError,
    *,
    model_path: str,
    write_started: bool,
) -> PiCompatOperationResult:
    if isinstance(error, FileNotFoundError):
        kind = PiCompatErrorKind.NOT_FOUND
    elif isinstance(error, PermissionError):
        kind = PiCompatErrorKind.PERMISSION_DENIED
    else:
        kind = PiCompatErrorKind.IO_ERROR
    return PiCompatOperationResult(
        model_text=sanitize_text(
            f"Could not edit file: {model_path}. Error code: {_error_code(error)}."
        ),
        truncated=False,
        error_kind=kind,
        exit_code=None,
        changed_path=None,
        side_effect_state=(
            PiCompatSideEffectState.COMPLETION_UNKNOWN
            if write_started
            else PiCompatSideEffectState.NOT_ATTEMPTED
        ),
    )


def _coerce_edits(edits: Sequence[Mapping[str, object]]) -> list[Edit] | None:
    coerced: list[Edit] = []
    for edit in edits:
        if not isinstance(edit, Mapping):
            return None
        old_text = edit.get("oldText")
        new_text = edit.get("newText")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            return None
        coerced.append(Edit(old_text, new_text))
    return coerced


def execute_edit(
    *,
    cwd: Path,
    path: str,
    edits: Sequence[Mapping[str, object]],
) -> PiCompatOperationResult:
    """Apply all Pi edits atomically after validating them against one snapshot."""

    if not _is_absolute_cwd(cwd):
        return _invalid_result("cwd must be an absolute path")
    if "\x00" in str(cwd):
        return _invalid_result("cwd must not contain NUL bytes")
    if not isinstance(path, str):
        return _invalid_result("path must be a string")
    if "\x00" in path:
        return _invalid_result("path must not contain NUL bytes")
    if isinstance(edits, (str, bytes)) or not isinstance(edits, Sequence):
        return _invalid_result("edits must be an array")
    typed_edits = _coerce_edits(edits)
    if typed_edits is None:
        return _invalid_result("each edit must contain string oldText and newText")
    if not typed_edits:
        return _invalid_result(
            "Edit tool input is invalid. edits must contain at least one replacement."
        )

    write_started = False
    try:
        absolute_path = resolve_to_cwd(path, cwd)
        with file_mutation_lock(absolute_path):
            try:
                raw_content = absolute_path.read_bytes().decode(
                    "utf-8", errors="replace"
                )
                bom, content = strip_bom(raw_content)
                original_ending = detect_line_ending(content)
                _, new_content = apply_edits_to_normalized_content(
                    normalize_to_lf(content), typed_edits, path
                )
                final_content = bom + restore_line_endings(new_content, original_ending)
                write_started = True
                with absolute_path.open("w", encoding="utf-8", newline="") as output:
                    output.write(final_content)
            except PiCompatEditError as error:
                return PiCompatOperationResult(
                    model_text=sanitize_text(str(error)),
                    truncated=False,
                    error_kind=PiCompatErrorKind.CONFLICT,
                    exit_code=None,
                    changed_path=None,
                    side_effect_state=PiCompatSideEffectState.NOT_ATTEMPTED,
                )
            except (OSError, UnicodeError) as error:
                return _filesystem_error_result(
                    error,
                    model_path=path,
                    write_started=write_started,
                )
    except _PathValidationError as error:
        return _invalid_result(str(error))
    except UnicodeError:
        return _invalid_result("path cannot be encoded by this filesystem")

    return PiCompatOperationResult(
        model_text=sanitize_text(
            f"Successfully replaced {len(typed_edits)} block(s) in {path}."
        ),
        truncated=False,
        error_kind=None,
        exit_code=None,
        changed_path=absolute_path,
        side_effect_state=PiCompatSideEffectState.CONFIRMED_COMPLETE,
    )
