"""External consumer surface used by the built-package typing smoke."""

from millforge import (
    HarnessExecutionRequest,
    HarnessExecutionResult,
    MillforgeBaseRunner,
    MillforgeBaseRunnerDescriptor,
    MillforgeInvocationEvidence,
)


def consume_public_runner_types(
    runner: MillforgeBaseRunner,
    request: HarnessExecutionRequest,
    result: HarnessExecutionResult,
    descriptor: MillforgeBaseRunnerDescriptor,
    evidence: MillforgeInvocationEvidence,
) -> tuple[
    MillforgeBaseRunner,
    HarnessExecutionRequest,
    HarnessExecutionResult,
    MillforgeBaseRunnerDescriptor,
    MillforgeInvocationEvidence,
]:
    return runner, request, result, descriptor, evidence
