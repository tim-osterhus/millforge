"""Pi-compatible path normalization and read-path recovery."""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlparse

__all__ = [
    "expand_path",
    "normalize_path",
    "resolve_read_path",
    "resolve_to_cwd",
]

_NARROW_NO_BREAK_SPACE = "\u202f"
_UNICODE_SPACES = re.compile(r"[\u00a0\u2000-\u200a\u202f\u205f\u3000]")
_MACOS_AM_PM = re.compile(r" (AM|PM)\.", re.IGNORECASE)


class _PathValidationError(ValueError):
    """Raised when a normalized path cannot be passed to this filesystem."""


def _is_absolute_cwd(cwd: object) -> bool:
    return isinstance(cwd, Path) and cwd.is_absolute()


def _file_url_path(value: str) -> str:
    parsed = urlparse(value)
    decoded_path = unquote(parsed.path)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", decoded_path):
        decoded_path = decoded_path[1:]
    if parsed.netloc and parsed.netloc != "localhost":
        return f"//{parsed.netloc}{decoded_path}"
    return decoded_path


def normalize_path(
    input_path: str,
    *,
    trim: bool = False,
    expand_tilde: bool = True,
    home_directory: Path | None = None,
    strip_at_prefix: bool = False,
    normalize_unicode_spaces: bool = False,
) -> str:
    """Normalize a user path using the pinned Pi path utility's options."""

    normalized = input_path.strip() if trim else input_path
    if normalize_unicode_spaces:
        normalized = _UNICODE_SPACES.sub(" ", normalized)
    if strip_at_prefix and normalized.startswith("@"):
        normalized = normalized[1:]

    if expand_tilde:
        home = str(home_directory if home_directory is not None else Path.home())
        if normalized == "~":
            return home
        if normalized.startswith("~/") or (
            os.name == "nt" and normalized.startswith("~\\")
        ):
            return os.path.join(home, normalized[2:])

    if normalized.startswith("file://"):
        return _file_url_path(normalized)
    return normalized


def expand_path(input_path: str) -> str:
    """Expand a Pi tool path without resolving it against a run cwd."""

    return normalize_path(
        input_path,
        normalize_unicode_spaces=True,
        strip_at_prefix=True,
    )


def _validate_filesystem_path(value: str) -> None:
    if "\x00" in value:
        raise _PathValidationError("path must not contain NUL bytes")
    try:
        os.fsencode(value)
    except UnicodeEncodeError as exc:
        raise _PathValidationError("path cannot be encoded by this filesystem") from exc


def resolve_to_cwd(file_path: str, cwd: Path) -> Path:
    """Resolve an absolute or run-relative Pi tool path without containment."""

    _validate_filesystem_path(file_path)
    try:
        normalized = expand_path(file_path)
        _validate_filesystem_path(normalized)
        candidate = Path(normalized)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        resolved = Path(os.path.abspath(candidate))
        _validate_filesystem_path(str(resolved))
    except _PathValidationError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise _PathValidationError("path is invalid") from exc
    return resolved


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except (OSError, UnicodeError, ValueError):
        return False


def _macos_screenshot_variant(path: Path) -> Path:
    return Path(
        _MACOS_AM_PM.sub(
            lambda match: f"{_NARROW_NO_BREAK_SPACE}{match[1]}.", str(path)
        )
    )


def _nfd_variant(path: Path) -> Path:
    return Path(unicodedata.normalize("NFD", str(path)))


def _curly_quote_variant(path: Path) -> Path:
    return Path(str(path).replace("'", "\u2019"))


def resolve_read_path(file_path: str, cwd: Path) -> Path:
    """Resolve a read path and try Pi's macOS filename recovery variants."""

    resolved = resolve_to_cwd(file_path, cwd)
    if _exists(resolved):
        return resolved

    am_pm_variant = _macos_screenshot_variant(resolved)
    if am_pm_variant != resolved and _exists(am_pm_variant):
        return am_pm_variant

    nfd_variant = _nfd_variant(resolved)
    if nfd_variant != resolved and _exists(nfd_variant):
        return nfd_variant

    curly_variant = _curly_quote_variant(resolved)
    if curly_variant != resolved and _exists(curly_variant):
        return curly_variant

    nfd_curly_variant = _curly_quote_variant(nfd_variant)
    if nfd_curly_variant != resolved and _exists(nfd_curly_variant):
        return nfd_curly_variant
    return resolved
