"""Shared validators for Millforge harness source contracts."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

HARNESS_ID_MAX_LENGTH = 160
POLICY_ID_MAX_LENGTH = 160
PROFILE_ID_MAX_LENGTH = 160
CANONICAL_TOOL_ID_MAX_LENGTH = 160
CAPABILITY_ID_MAX_LENGTH = CANONICAL_TOOL_ID_MAX_LENGTH
NODE_ID_MAX_LENGTH = 64
ARTIFACT_ID_MAX_LENGTH = 128
TERMINAL_RESULT_MAX_LENGTH = 128
ARGUMENT_NAME_MAX_LENGTH = 128
REQUEST_ID_MAX_LENGTH = 160
TOOL_VERSION_MAX = 2_147_483_647
HARNESS_VERSION_MAX = 2_147_483_647

_DOTTED_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ARTIFACT_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_TERMINAL_RESULT_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_ARGUMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_TOOL_REF_RE = re.compile(
    r"^(?P<tool_id>[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*)@(?P<version>[0-9]+)$"
)
_LOWER_FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ToolReference:
    """Parsed exact-version tool reference."""

    tool_id: str
    version: int


def validate_nonblank(value: str, field_name: str) -> str:
    """Validate a strict string is nonblank without normalizing it."""
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def validate_utf8_size(value: str, field_name: str, maximum: int) -> str:
    """Validate a string's UTF-8 encoded size."""
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} UTF-8 bytes")
    return value


def validate_unique(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    """Validate a tuple has no duplicate values."""
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} values must be unique")
    return values


def validate_harness_id(value: str) -> str:
    return _validate_pattern(value, "harness_id", _DOTTED_ID_RE, HARNESS_ID_MAX_LENGTH)


def validate_policy_id(value: str) -> str:
    return _validate_pattern(value, "policy_id", _DOTTED_ID_RE, POLICY_ID_MAX_LENGTH)


def validate_profile_id(value: str) -> str:
    return _validate_pattern(value, "profile_id", _DOTTED_ID_RE, PROFILE_ID_MAX_LENGTH)


def validate_canonical_tool_id(value: str) -> str:
    return _validate_pattern(
        value, "tool_id", _DOTTED_ID_RE, CANONICAL_TOOL_ID_MAX_LENGTH
    )


def validate_capability_id(value: str) -> str:
    return _validate_pattern(
        value, "capability_id", _DOTTED_ID_RE, CAPABILITY_ID_MAX_LENGTH
    )


def validate_stage_kind_id(value: str) -> str:
    return _validate_pattern(
        value, "stage_kind_id", _DOTTED_ID_RE, POLICY_ID_MAX_LENGTH
    )


def validate_request_id(value: str) -> str:
    return _validate_pattern(value, "request_id", _DOTTED_ID_RE, REQUEST_ID_MAX_LENGTH)


def validate_node_id(value: str) -> str:
    return _validate_pattern(value, "node_id", _NODE_ID_RE, NODE_ID_MAX_LENGTH)


def validate_artifact_id(value: str) -> str:
    return _validate_pattern(
        value, "artifact_id", _ARTIFACT_ID_RE, ARTIFACT_ID_MAX_LENGTH
    )


def validate_terminal_result(value: str) -> str:
    return _validate_pattern(
        value, "terminal_result", _TERMINAL_RESULT_RE, TERMINAL_RESULT_MAX_LENGTH
    )


def validate_argument_name(value: str) -> str:
    return _validate_pattern(
        value, "argument_name", _ARGUMENT_NAME_RE, ARGUMENT_NAME_MAX_LENGTH
    )


def validate_lower_field_key(value: str) -> str:
    return _validate_pattern(value, "key", _LOWER_FIELD_KEY_RE, 64)


def validate_sha256(value: str, field_name: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be exactly 64 lowercase hex characters")
    return value


def validate_harness_version(value: int) -> int:
    if value < 1 or value > HARNESS_VERSION_MAX:
        raise ValueError("harness_version must be in range 1..2147483647")
    return value


def validate_tool_version(value: int) -> int:
    if value < 1 or value > TOOL_VERSION_MAX:
        raise ValueError("tool_version must be in range 1..2147483647")
    return value


def validate_tool_reference(value: str) -> str:
    parse_tool_reference(value)
    return value


def parse_tool_reference(value: str) -> ToolReference:
    """Parse and validate ``<canonical tool ID>@<positive integer version>``."""
    match = _TOOL_REF_RE.fullmatch(value)
    if match is None:
        raise ValueError("tool_ref must be an exact-version tool reference")
    tool_id = validate_canonical_tool_id(match.group("tool_id"))
    version_text = match.group("version")
    if len(version_text) > 1 and version_text.startswith("0"):
        raise ValueError("tool_ref version must not contain leading zeroes")
    version = int(version_text)
    if version < 1 or version > TOOL_VERSION_MAX:
        raise ValueError("tool_ref version must be in range 1..2147483647")
    return ToolReference(tool_id=tool_id, version=version)


def validate_threshold(value: object, field_name: str) -> float:
    """Validate a context threshold scalar.

    Integer values are accepted for explicitly float-valued thresholds, but
    strings and booleans are not coerced.
    """
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{field_name} must be a finite number")
    threshold = float(value)
    if not math.isfinite(threshold):
        raise ValueError(f"{field_name} must be finite")
    if threshold <= 0 or threshold > 1:
        raise ValueError(f"{field_name} must be in (0, 1]")
    return threshold


def _validate_pattern(
    value: str, field_name: str, pattern: re.Pattern[str], maximum: int
) -> str:
    if len(value) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    if pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} has invalid format")
    return value
