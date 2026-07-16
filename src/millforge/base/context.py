"""Pi-derived project-context discovery for ``millforge-base``."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
)

__all__ = [
    "MillforgeBaseContextFile",
    "MillforgeBaseContextSnapshot",
    "load_millforge_base_context",
]

_CONTEXT_BYTE_LIMIT = 49_152
_DIAGNOSTIC_BYTE_LIMIT = 2_048
_CONTEXT_CANDIDATES = ("AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _truncate_to_utf8_bytes(value: str, limit: int) -> str:
    """Keep a valid UTF-8 prefix within ``limit`` bytes."""

    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _bounded_diagnostic(value: str) -> str:
    bounded = _truncate_to_utf8_bytes(value, _DIAGNOSTIC_BYTE_LIMIT)
    return bounded or "unreadable file"


def _validate_sha256(value: str, field_name: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be exactly 64 lowercase hex characters")
    return value


def _validate_diagnostics(value: tuple[str, ...]) -> tuple[str, ...]:
    for diagnostic in value:
        if not diagnostic.strip():
            raise ValueError("diagnostics must be nonblank")
        if len(diagnostic.encode("utf-8")) > _DIAGNOSTIC_BYTE_LIMIT:
            raise ValueError("diagnostics may contain at most 2048 UTF-8 bytes")
    return value


class MillforgeBaseContextFile(BaseModel):
    """One included private project-context record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["global", "project"]
    path: Path
    content: StrictStr
    raw_sha256: StrictStr
    original_byte_count: StrictInt = Field(ge=0)
    included_byte_count: StrictInt = Field(ge=0)
    truncated: StrictBool

    @field_validator("raw_sha256")
    @classmethod
    def _raw_sha256_is_valid(cls, value: str) -> str:
        return _validate_sha256(value, "raw_sha256")


class MillforgeBaseContextSnapshot(BaseModel):
    """The bounded, ordered private context available to prompt assembly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    files: tuple[MillforgeBaseContextFile, ...]
    diagnostics: tuple[StrictStr, ...]
    context_sha256: StrictStr
    truncated: StrictBool

    @field_validator("diagnostics")
    @classmethod
    def _diagnostics_are_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_diagnostics(value)

    @field_validator("context_sha256")
    @classmethod
    def _context_sha256_is_valid(cls, value: str) -> str:
        return _validate_sha256(value, "context_sha256")


@dataclass(frozen=True)
class _DiscoveredContextFile:
    scope: Literal["global", "project"]
    path: Path
    raw: bytes
    content: str


def _resolved_absolute_path(path: Path, field_name: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{field_name} must be absolute")
    return path.resolve()


def _context_file_from_directory(
    directory: Path,
    scope: Literal["global", "project"],
    diagnostics: list[str],
) -> _DiscoveredContextFile | None:
    for filename in _CONTEXT_CANDIDATES:
        candidate = (directory / filename).resolve()
        if not candidate.exists():
            continue
        try:
            raw = candidate.read_bytes()
        except OSError as error:
            diagnostics.append(
                _bounded_diagnostic(f"Could not read {candidate}: {error}")
            )
            continue
        return _DiscoveredContextFile(
            scope=scope,
            path=candidate,
            raw=raw,
            content=raw.decode("utf-8", errors="replace"),
        )
    return None


def _project_directories(cwd: Path) -> tuple[Path, ...]:
    return tuple(reversed(cwd.parents)) + (cwd,)


def _context_sha256(files: tuple[MillforgeBaseContextFile, ...]) -> str:
    payload = [
        {
            "scope": file.scope,
            "path": file.path.as_posix(),
            "content": file.content,
            "raw_sha256": file.raw_sha256,
            "original_byte_count": file.original_byte_count,
            "included_byte_count": file.included_byte_count,
            "truncated": file.truncated,
        }
        for file in files
    ]
    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _allocate_context(
    discovered_files: tuple[_DiscoveredContextFile, ...],
) -> tuple[tuple[MillforgeBaseContextFile, ...], bool]:
    remaining = _CONTEXT_BYTE_LIMIT
    included: list[MillforgeBaseContextFile] = []

    for index, discovered in enumerate(discovered_files):
        content_bytes = len(discovered.content.encode("utf-8"))
        raw_sha256 = hashlib.sha256(discovered.raw).hexdigest()
        if content_bytes <= remaining:
            included.append(
                MillforgeBaseContextFile(
                    scope=discovered.scope,
                    path=discovered.path,
                    content=discovered.content,
                    raw_sha256=raw_sha256,
                    original_byte_count=len(discovered.raw),
                    included_byte_count=content_bytes,
                    truncated=False,
                )
            )
            remaining -= content_bytes
            if remaining == 0 and index < len(discovered_files) - 1:
                return tuple(included), True
            continue

        content = _truncate_to_utf8_bytes(discovered.content, remaining)
        included.append(
            MillforgeBaseContextFile(
                scope=discovered.scope,
                path=discovered.path,
                content=content,
                raw_sha256=raw_sha256,
                original_byte_count=len(discovered.raw),
                included_byte_count=len(content.encode("utf-8")),
                truncated=True,
            )
        )
        return tuple(included), True

    return tuple(included), False


def load_millforge_base_context(
    *, cwd: Path, home_directory: Path, enabled: bool
) -> MillforgeBaseContextSnapshot:
    """Load Pi-ordered context records under Millforge's finite byte bound."""

    resolved_cwd = _resolved_absolute_path(cwd, "cwd")
    resolved_home = _resolved_absolute_path(home_directory, "home_directory")
    return _load_millforge_base_context_resolved(
        cwd=resolved_cwd,
        home_directory=resolved_home,
        enabled=enabled,
    )


def _load_millforge_base_context_resolved(
    *, cwd: Path, home_directory: Path, enabled: bool
) -> MillforgeBaseContextSnapshot:
    if not enabled:
        empty_files: tuple[MillforgeBaseContextFile, ...] = ()
        return MillforgeBaseContextSnapshot(
            files=empty_files,
            diagnostics=(),
            context_sha256=_context_sha256(empty_files),
            truncated=False,
        )

    diagnostics: list[str] = []
    discovered: list[_DiscoveredContextFile] = []
    native_global = _context_file_from_directory(
        home_directory / ".millforge", "global", diagnostics
    )
    if native_global is None:
        native_global = _context_file_from_directory(
            home_directory / ".pi" / "agent", "global", diagnostics
        )
    if native_global is not None:
        discovered.append(native_global)

    seen_paths = {file.path for file in discovered}
    for directory in _project_directories(cwd):
        project_file = _context_file_from_directory(directory, "project", diagnostics)
        if project_file is None or project_file.path in seen_paths:
            continue
        discovered.append(project_file)
        seen_paths.add(project_file.path)

    files, truncated = _allocate_context(tuple(discovered))
    return MillforgeBaseContextSnapshot(
        files=files,
        diagnostics=tuple(diagnostics),
        context_sha256=_context_sha256(files),
        truncated=truncated,
    )
