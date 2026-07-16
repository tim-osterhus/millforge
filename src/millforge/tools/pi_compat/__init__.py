"""Source-attributed Pi 0.79.6 coding-tool compatibility operations."""

from .contracts import (
    PiCompatErrorKind,
    PiCompatOperationResult,
    PiCompatSideEffectState,
)
from .editing import execute_edit
from .operations import execute_ls, execute_read, execute_write
from .process import (
    PiCompatCancellation,
    PiCompatShellConfig,
    PiCompatShellResolutionError,
    execute_bash,
    resolve_pi_compat_shell,
)
from .search import execute_find, execute_grep

__all__ = [
    "PiCompatCancellation",
    "PiCompatErrorKind",
    "PiCompatOperationResult",
    "PiCompatShellConfig",
    "PiCompatShellResolutionError",
    "PiCompatSideEffectState",
    "execute_bash",
    "execute_edit",
    "execute_find",
    "execute_grep",
    "execute_ls",
    "execute_read",
    "execute_write",
    "resolve_pi_compat_shell",
]
