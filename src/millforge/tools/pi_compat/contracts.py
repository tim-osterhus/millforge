"""Shared contracts for the Pi 0.79.6 compatibility operation pack."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "PiCompatErrorKind",
    "PiCompatOperationResult",
    "PiCompatSideEffectState",
]


class PiCompatErrorKind(str, Enum):
    """Closed operation errors translated by the unrestricted tool adapter."""

    INVALID_ARGUMENTS = "invalid_arguments"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    CONFLICT = "conflict"
    IO_ERROR = "io_error"
    PROCESS_EXIT_NONZERO = "process_exit_nonzero"
    PROCESS_TIMEOUT = "process_timeout"
    CANCELLED = "cancelled"
    PROCESS_LAUNCH_ERROR = "process_launch_error"


class PiCompatSideEffectState(str, Enum):
    """The operation's explicit side-effect certainty."""

    NOT_ATTEMPTED = "not_attempted"
    CONFIRMED_ABSENT = "confirmed_absent"
    CONFIRMED_COMPLETE = "confirmed_complete"
    ROLLED_BACK = "rolled_back"
    COMPLETION_UNKNOWN = "completion_unknown"


@dataclass(frozen=True)
class PiCompatOperationResult:
    """The complete model-visible result of one Pi-compatible operation."""

    model_text: str
    truncated: bool
    error_kind: PiCompatErrorKind | None
    exit_code: int | None
    changed_path: Path | None
    side_effect_state: PiCompatSideEffectState
