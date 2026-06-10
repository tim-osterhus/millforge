"""Millforge — a reliability layer for self-hosted LLM tool-calling."""

__version__ = "0.1.0"

from millforge.contracts import (
    ArtifactRef,
    CancellationRef,
    CapabilityEnvelope,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    DiagnosticMetadata,
    GuardedSessionRequest,
    GuardedSessionResult,
    HarnessExecutionResult,
    ModelProfile,
    RunDirRef,
    SecretRef,
    StageExecutionRequest,
    TerminalIntent,
    TimeoutRef,
    TimingMetadata,
    UsageMetadata,
    ValidatedModelRequest,
    ValidatedModelResponse,
    ValidatedToolCall,
    ValidatedToolResult,
)
from millforge.exceptions import (
    ArtifactWriteError,
    BackendTranslationError,
    DeadlineExceededError,
    HarnessMismatchError,
    MillforgeConfigError,
    MillforgeError,
    ModelTransportError,
    OperationCancelledError,
    ToolInvokeError,
)
from millforge.protocols import (
    GuardrailBackend,
    HarnessRuntime,
    ModelClient,
    ToolExecutor,
)

__all__: list[str] = [
    # Contracts
    "ArtifactRef",
    "CancellationRef",
    "CapabilityEnvelope",
    "CompiledHarnessHash",
    "CompiledHarnessIdentity",
    "DiagnosticMetadata",
    "GuardedSessionRequest",
    "GuardedSessionResult",
    "HarnessExecutionResult",
    "ModelProfile",
    "RunDirRef",
    "SecretRef",
    "StageExecutionRequest",
    "TerminalIntent",
    "TimeoutRef",
    "TimingMetadata",
    "UsageMetadata",
    "ValidatedModelRequest",
    "ValidatedModelResponse",
    "ValidatedToolCall",
    "ValidatedToolResult",
    # Exceptions
    "ArtifactWriteError",
    "BackendTranslationError",
    "DeadlineExceededError",
    "HarnessMismatchError",
    "MillforgeConfigError",
    "MillforgeError",
    "ModelTransportError",
    "OperationCancelledError",
    "ToolInvokeError",
    # Protocols
    "GuardrailBackend",
    "HarnessRuntime",
    "ModelClient",
    "ToolExecutor",
]
