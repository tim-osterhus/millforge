"""Pi-derived system-prompt assembly for ``millforge-base``."""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
)

from millforge.base.context import (
    MillforgeBaseContextSnapshot,
    _bounded_diagnostic,
    _resolved_absolute_path,
    _truncate_to_utf8_bytes,
    _validate_diagnostics,
    _validate_sha256,
)
from millforge.base.options import MillforgeBaseOptions

__all__ = [
    "MillforgeBasePromptBudgetError",
    "MillforgeBasePromptSnapshot",
    "build_millforge_base_system_prompt",
]

_PROMPT_BYTE_LIMIT = 65_536
_APPEND_PREFIX = "\n\n"
_CONTEXT_PREFIX = (
    "\n\n<project_context>\n\nProject-specific instructions and guidelines:\n\n"
)
_CONTEXT_SUFFIX = "</project_context>\n"

_DEFAULT_SYSTEM_PROMPT = (
    "You are an expert coding assistant operating inside Millforge, a coding agent "
    "harness. You help users by reading files, executing commands, editing code, "
    "and writing new files.\n\n"
    "Available tools:\n"
    "- read: Read file contents\n"
    "- bash: Execute commands in the resolved host shell\n"
    "- edit: Make precise file edits, including multiple disjoint edits in one call\n"
    "- write: Create or overwrite files\n"
    "- grep: Search file contents for patterns (respects .gitignore)\n"
    "- find: Find files by glob pattern (respects .gitignore)\n"
    "- ls: List directory contents\n"
    "- submit: Complete the current run\n"
    "- block: Report that the current run is blocked\n"
    "- reject: Reject the current run\n\n"
    "Guidelines:\n"
    "- Use write only for new files or complete rewrites.\n"
    "- Use edit for precise changes; every edits[].oldText must uniquely match the "
    "original file.\n"
    "- Put multiple disjoint changes to one file in one edit call and never overlap "
    "edit regions.\n"
    "- Be concise in your responses.\n"
    "- Show file paths clearly when working with files.\n"
    "- Filesystem and shell tools run with the permissions of the Millforge process "
    "and are not sandboxed.\n"
    "- Finish by calling submit, block, or reject with the corresponding fixed "
    "terminal_result and a concise summary."
)


class MillforgeBasePromptBudgetError(ValueError):
    """Raised when the immutable prompt footer cannot fit its byte budget."""


class MillforgeBasePromptSnapshot(BaseModel):
    """The final bounded system prompt and its deterministic accounting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    system_instructions: StrictStr
    effective_prompt_sha256: StrictStr
    byte_count: StrictInt = Field(ge=0)
    prompt_date: datetime.date
    truncated: StrictBool
    diagnostics: tuple[StrictStr, ...]

    @field_validator("effective_prompt_sha256")
    @classmethod
    def _effective_prompt_sha256_is_valid(cls, value: str) -> str:
        return _validate_sha256(value, "effective_prompt_sha256")

    @field_validator("diagnostics")
    @classmethod
    def _diagnostics_are_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_diagnostics(value)


@dataclass(frozen=True)
class _PromptFile:
    content: str
    raw_sha256: str


def _prompt_candidates(
    cwd: Path, home_directory: Path, filename: str
) -> tuple[Path, ...]:
    return (
        cwd / ".millforge" / filename,
        cwd / ".pi" / filename,
        home_directory / ".millforge" / filename,
        home_directory / ".pi" / "agent" / filename,
    )


def _discover_prompt_file(
    *, cwd: Path, home_directory: Path, filename: str, diagnostics: list[str]
) -> _PromptFile | None:
    for candidate in _prompt_candidates(cwd, home_directory, filename):
        resolved = candidate.resolve()
        if not resolved.exists():
            continue
        try:
            raw = resolved.read_bytes()
        except OSError as error:
            diagnostics.append(
                _bounded_diagnostic(f"Could not read {resolved}: {error}")
            )
            continue
        return _PromptFile(
            content=_truncate_to_utf8_bytes(
                raw.decode("utf-8", errors="replace"), _PROMPT_BYTE_LIMIT
            ),
            raw_sha256=hashlib.sha256(raw).hexdigest(),
        )
    return None


def _footer(*, prompt_date: datetime.date, cwd: Path) -> str:
    return (
        f"\nCurrent date: {prompt_date.isoformat()}"
        f"\nCurrent working directory: {cwd.as_posix()}"
    )


def _record_prefix(path: Path) -> str:
    return f'<project_instructions path="{path.as_posix()}">\n'


def _snapshot(
    *,
    prompt: str,
    prompt_date: datetime.date,
    truncated: bool,
    diagnostics: tuple[str, ...],
) -> MillforgeBasePromptSnapshot:
    encoded = prompt.encode("utf-8")
    return MillforgeBasePromptSnapshot(
        system_instructions=prompt,
        effective_prompt_sha256=hashlib.sha256(encoded).hexdigest(),
        byte_count=len(encoded),
        prompt_date=prompt_date,
        truncated=truncated,
        diagnostics=diagnostics,
    )


def build_millforge_base_system_prompt(
    *,
    options: MillforgeBaseOptions,
    context: MillforgeBaseContextSnapshot,
    cwd: Path,
    home_directory: Path,
    prompt_date: datetime.date,
) -> MillforgeBasePromptSnapshot:
    """Assemble the exact finite Pi-derived prompt order and framing."""

    resolved_cwd = _resolved_absolute_path(cwd, "cwd")
    resolved_home = _resolved_absolute_path(home_directory, "home_directory")
    return _build_millforge_base_system_prompt_resolved(
        options=options,
        context=context,
        cwd=resolved_cwd,
        home_directory=resolved_home,
        prompt_date=prompt_date,
    )


def _build_millforge_base_system_prompt_resolved(
    *,
    options: MillforgeBaseOptions,
    context: MillforgeBaseContextSnapshot,
    cwd: Path,
    home_directory: Path,
    prompt_date: datetime.date,
) -> MillforgeBasePromptSnapshot:
    diagnostics = list(context.diagnostics)

    if options.system_prompt is None:
        discovered_body = _discover_prompt_file(
            cwd=cwd,
            home_directory=home_directory,
            filename="SYSTEM.md",
            diagnostics=diagnostics,
        )
        body = (
            _DEFAULT_SYSTEM_PROMPT
            if discovered_body is None
            else discovered_body.content
        )
    else:
        body = options.system_prompt

    if options.append_system_prompt is None:
        discovered_append = _discover_prompt_file(
            cwd=cwd,
            home_directory=home_directory,
            filename="APPEND_SYSTEM.md",
            diagnostics=diagnostics,
        )
        append = None if discovered_append is None else discovered_append.content
    else:
        append = options.append_system_prompt

    footer = _footer(prompt_date=prompt_date, cwd=cwd)
    footer_bytes = len(footer.encode("utf-8"))
    if footer_bytes > _PROMPT_BYTE_LIMIT:
        raise MillforgeBasePromptBudgetError(
            "millforge-base prompt footer exceeds 65536 UTF-8 bytes"
        )

    body_budget = _PROMPT_BYTE_LIMIT - footer_bytes
    bounded_body = _truncate_to_utf8_bytes(body, body_budget)
    diagnostics_tuple = tuple(diagnostics)
    if bounded_body != body:
        return _snapshot(
            prompt=bounded_body + footer,
            prompt_date=prompt_date,
            truncated=True,
            diagnostics=diagnostics_tuple,
        )

    prompt = bounded_body
    if append:
        if len((prompt + _APPEND_PREFIX + footer).encode("utf-8")) > _PROMPT_BYTE_LIMIT:
            return _snapshot(
                prompt=prompt + footer,
                prompt_date=prompt_date,
                truncated=True,
                diagnostics=diagnostics_tuple,
            )
        append_budget = _PROMPT_BYTE_LIMIT - len(
            (prompt + _APPEND_PREFIX + footer).encode("utf-8")
        )
        bounded_append = _truncate_to_utf8_bytes(append, append_budget)
        prompt += _APPEND_PREFIX + bounded_append
        if bounded_append != append:
            return _snapshot(
                prompt=prompt + footer,
                prompt_date=prompt_date,
                truncated=True,
                diagnostics=diagnostics_tuple,
            )

    if not context.files:
        return _snapshot(
            prompt=prompt + footer,
            prompt_date=prompt_date,
            truncated=False,
            diagnostics=diagnostics_tuple,
        )

    if (
        len((prompt + _CONTEXT_PREFIX + _CONTEXT_SUFFIX + footer).encode("utf-8"))
        > _PROMPT_BYTE_LIMIT
    ):
        return _snapshot(
            prompt=prompt + footer,
            prompt_date=prompt_date,
            truncated=True,
            diagnostics=diagnostics_tuple,
        )

    prompt += _CONTEXT_PREFIX
    for context_file in context.files:
        prefix = _record_prefix(context_file.path)
        suffix = "\n</project_instructions>\n\n"
        if (
            len((prompt + prefix + suffix + _CONTEXT_SUFFIX + footer).encode("utf-8"))
            > _PROMPT_BYTE_LIMIT
        ):
            return _snapshot(
                prompt=prompt + _CONTEXT_SUFFIX + footer,
                prompt_date=prompt_date,
                truncated=True,
                diagnostics=diagnostics_tuple,
            )
        content_budget = _PROMPT_BYTE_LIMIT - len(
            (prompt + prefix + suffix + _CONTEXT_SUFFIX + footer).encode("utf-8")
        )
        bounded_content = _truncate_to_utf8_bytes(context_file.content, content_budget)
        prompt += prefix + bounded_content + suffix
        if bounded_content != context_file.content:
            return _snapshot(
                prompt=prompt + _CONTEXT_SUFFIX + footer,
                prompt_date=prompt_date,
                truncated=True,
                diagnostics=diagnostics_tuple,
            )

    return _snapshot(
        prompt=prompt + _CONTEXT_SUFFIX + footer,
        prompt_date=prompt_date,
        truncated=False,
        diagnostics=diagnostics_tuple,
    )
