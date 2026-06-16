"""Contained logical path policy for built-in runtime tools."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path, PurePosixPath

_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_WINDOWS_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "//?/", "//./")
_UNC_PREFIXES = ("\\\\", "//")
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    {
        "aux",
        "con",
        "conin$",
        "conout$",
        "nul",
        "prn",
        *{f"com{i}" for i in range(1, 10)},
        *{f"lpt{i}" for i in range(1, 10)},
    }
)


class PathPolicyError(ValueError):
    """Raised when a model-provided logical path is outside policy."""


def validate_logical_path(value: str, *, allow_dot: bool = False) -> PurePosixPath:
    """Validate a model-visible logical path before host resolution."""
    if not isinstance(value, str) or not value.strip():
        raise PathPolicyError("path must be a non-empty logical relative path")
    if "\x00" in value:
        raise PathPolicyError("path contains a NUL byte")
    if "\\" in value:
        raise PathPolicyError("path must use POSIX separators")
    if ":" in value:
        raise PathPolicyError("path must not contain Windows drive or ADS syntax")
    if value.startswith(_WINDOWS_DEVICE_PREFIXES) or value.startswith(_UNC_PREFIXES):
        raise PathPolicyError("path must not use Windows device or UNC syntax")
    if _WINDOWS_DRIVE_RE.match(value):
        raise PathPolicyError("path must not use a Windows drive prefix")
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("/"):
        raise PathPolicyError("path must not be absolute")
    if value.startswith("~"):
        raise PathPolicyError("path must not use home-directory syntax")
    parts = path.parts
    if any(part in ("", "..") for part in parts):
        raise PathPolicyError("path must not contain traversal components")
    if not allow_dot and any(part == "." for part in parts):
        raise PathPolicyError("path must not contain current-directory components")
    if parts == (".",) and not allow_dot:
        raise PathPolicyError("path must not be current directory")
    _reject_windows_reserved_device_names(parts)
    return path


def resolve_existing_contained(
    root: Path, logical: str, *, allow_dot: bool = False
) -> Path:
    """Resolve an existing logical path under a trusted root."""
    path = validate_logical_path(logical, allow_dot=allow_dot)
    root_resolved = root.resolve(strict=True)
    candidate = (root_resolved / Path(*path.parts)).resolve(strict=True)
    _ensure_contained(root_resolved, candidate)
    return candidate


def resolve_write_contained(root: Path, logical: str) -> Path:
    """Resolve a writable logical file path under a trusted root."""
    path = validate_logical_path(logical)
    root_resolved = root.resolve(strict=True)
    candidate = root_resolved / Path(*path.parts)
    parent = candidate.parent.resolve(strict=True)
    _ensure_contained(root_resolved, parent)
    if candidate.exists() or candidate.is_symlink():
        resolved = candidate.resolve(strict=True)
        _ensure_contained(root_resolved, resolved)
        if resolved.is_dir():
            raise PathPolicyError("path resolves to a directory")
        return resolved
    return parent / candidate.name


def atomic_write_contained(root: Path, logical: str, content: bytes) -> str:
    """Atomically write bytes to a contained logical path and return sha256."""
    target = resolve_write_contained(root, logical)
    digest = canonical_sha256_bytes(content)
    fd: int | None = None
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=f".{target.name}.tmp.",
        )
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return digest


def canonical_sha256_bytes(content: bytes) -> str:
    """Hash raw bytes using the same lowercase SHA-256 format as JSON results."""
    import hashlib

    return hashlib.sha256(content).hexdigest()


def logical_from_resolved(root: Path, target: Path) -> str:
    """Return a POSIX logical path after containment has already been enforced."""
    root_resolved = root.resolve(strict=True)
    resolved = target.resolve(strict=True)
    _ensure_contained(root_resolved, resolved)
    return resolved.relative_to(root_resolved).as_posix()


def _ensure_contained(root: Path, candidate: Path) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PathPolicyError("path resolves outside the trusted root") from exc


def _reject_windows_reserved_device_names(parts: tuple[str, ...]) -> None:
    for part in parts:
        if part in (".", ".."):
            continue
        if _is_windows_reserved_device_name(part):
            raise PathPolicyError("path must not use a reserved Windows device name")


def _is_windows_reserved_device_name(part: str) -> bool:
    trimmed = part.rstrip(" .")
    if not trimmed:
        return False
    base = trimmed.split(".", 1)[0].rstrip(" .").casefold()
    return base in _WINDOWS_RESERVED_DEVICE_NAMES
