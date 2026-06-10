"""Exception hierarchy for the millforge library.

All millforge-specific exceptions are subclasses of `MillforgeError`.
Downstream code should catch ``MillforgeError`` when handling any
expected millforge failure mode, or catch specific subclasses for
targeted recovery.
"""

from __future__ import annotations

from typing import Optional


class MillforgeError(Exception):
    """Base exception for all millforge errors.

    All millforge-specific exceptions inherit from this class so that
    downstream code can catch ``MillforgeError`` as a blanket handler.
    """

    def __init__(self, message: str, *, cause: Optional[Exception] = None) -> None:
        super().__init__(message)
        self._cause = cause
        if cause is not None:
            self.__cause__ = cause

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self._cause is not None:
            parts.append(f"[caused by: {self._cause}]")
        return " ".join(parts)


class MillforgeConfigError(MillforgeError):
    """Configuration or contract validation error.

    Raised when millforge configuration values are invalid, missing, or
    inconsistent, or when a runtime contract validation fails.
    """


class HarnessMismatchError(MillforgeError):
    """Compiled-harness mismatch error.

    Raised when the runtime environment does not match the harness
    definition that a plan or artifact was compiled against.
    """


class BackendTranslationError(MillforgeError):
    """Backend translation error.

    Raised when a request or response cannot be translated between
    millforge's internal representation and a backend-specific format.
    """


class ModelTransportError(MillforgeError):
    """Model transport error.

    Raised when a model request cannot be sent or a response cannot be
    received over the transport layer (HTTP, IPC, etc.).
    """


class ToolInvokeError(MillforgeError):
    """Tool execution error.

    Raised when a tool callable raises during invocation or returns an
    unexpected result.
    """


class DeadlineExceededError(MillforgeError):
    """Operation deadline exceeded.

    Raised when an operation (model call, tool execution, workflow step)
    exceeds its configured timeout.
    """


class OperationCancelledError(MillforgeError):
    """Operation was cancelled.

    Raised when an operation is cancelled via an explicit cancellation
    signal before completion.
    """


class ArtifactWriteError(MillforgeError):
    """Artifact write error.

    Raised when an artifact cannot be written to its target location,
    including permission errors, disk-full conditions, or serialisation
    failures.
    """
