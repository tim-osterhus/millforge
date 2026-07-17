"""Stable in-process invocation facade for composed Millforge base components."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Literal, Protocol, cast

from millforge.artifacts import RuntimeArtifactWriter as _AtomicRuntimeArtifactWriter
from millforge.contracts import (
    HarnessExecutionRequest,
    HarnessExecutionResult,
)
from millforge.exceptions import MillforgeConfigError
from millforge.protocols import (
    CancellationResolver,
    ModelClient,
    RuntimeArtifactWriter,
    RuntimeClock,
)
from .composition import MillforgeBaseComponents
from .identity import (
    MillforgeBaseRunnerDescriptor,
    MillforgeInvocationEvidence,
    _build_invocation_evidence,
    _descriptor_agrees_with_components,
    _has_valid_descriptor_digest,
    _has_valid_invocation_digest,
    describe_millforge_base,
)

__all__ = [
    "RuntimeArtifactWriterFactory",
    "default_runtime_artifact_writer_factory",
    "MillforgeBaseRuntimeServices",
    "MillforgeBaseBindingError",
    "MillforgeBaseRunner",
    "create_millforge_base_runner",
]

_BindingReason = Literal[
    "harness_identity",
    "compiled_plan_hash",
    "stage_kind",
    "model_profile",
    "capability_envelope",
    "backend_composition",
    "descriptor_hash",
    "descriptor_composition",
    "invocation_evidence_hash",
    "invocation_evidence_composition",
]

_BINDING_MESSAGES: dict[_BindingReason, str] = {
    "harness_identity": "Execution request harness identity does not match millforge-base components",
    "compiled_plan_hash": "Execution request plan hash does not match millforge-base components",
    "stage_kind": "Execution request stage kind is not admitted by millforge-base components",
    "model_profile": "Execution request model profile does not match millforge-base components",
    "capability_envelope": "Execution request capabilities do not match millforge-base components",
    "backend_composition": "Millforge-base components are not internally consistent",
    "descriptor_hash": "Millforge-base runner descriptor digest is invalid",
    "descriptor_composition": "Millforge-base descriptor does not match its components",
    "invocation_evidence_hash": "Millforge-base invocation evidence digest is invalid",
    "invocation_evidence_composition": "Millforge-base invocation evidence does not match its components",
}


class RuntimeArtifactWriterFactory(Protocol):
    """Construct an artifact writer scoped to one request run directory."""

    def __call__(self, run_directory: Path) -> RuntimeArtifactWriter: ...


def default_runtime_artifact_writer_factory(
    run_directory: Path,
) -> RuntimeArtifactWriter:
    """Construct the default atomic artifact writer for ``run_directory``."""
    return _AtomicRuntimeArtifactWriter(run_directory)


@dataclass(frozen=True, slots=True)
class MillforgeBaseRuntimeServices:
    """Caller-owned runtime services shared across base runner invocations."""

    model_client: ModelClient
    clock: RuntimeClock
    cancellation_resolver: CancellationResolver
    artifact_writer_factory: RuntimeArtifactWriterFactory = (
        default_runtime_artifact_writer_factory
    )


class MillforgeBaseBindingError(MillforgeConfigError):
    """A request or component binding does not match the pinned base plan."""

    reason: _BindingReason

    def __init__(self, reason: _BindingReason) -> None:
        self.reason = reason
        super().__init__(_BINDING_MESSAGES[reason])


class _BaseInvocationExecutor(Protocol):
    async def execute_base_invocation(
        self,
        *,
        components: MillforgeBaseComponents,
        request: HarnessExecutionRequest,
        model_client: ModelClient,
        clock: RuntimeClock,
        cancellation_resolver: CancellationResolver,
        artifact_writer_factory: RuntimeArtifactWriterFactory,
    ) -> HarnessExecutionResult: ...


def _load_invocation_executor() -> _BaseInvocationExecutor:
    module_name = ".".join(("millforge", "_forge", "base_runner"))
    return cast(_BaseInvocationExecutor, import_module(module_name))


class MillforgeBaseRunner:
    """Execute requests against one immutable Millforge base composition."""

    __slots__ = ("_components", "_descriptor", "_invocation_evidence", "_services")

    def __init__(
        self,
        *,
        components: MillforgeBaseComponents,
        services: MillforgeBaseRuntimeServices,
    ) -> None:
        self._components = components
        self._services = services
        self._descriptor = describe_millforge_base()
        self._verify_component_composition()
        try:
            self._invocation_evidence = _build_invocation_evidence(
                components, self._descriptor
            )
        except (TypeError, ValueError):
            raise MillforgeBaseBindingError("invocation_evidence_composition") from None
        self._verify_identity_evidence()

    @property
    def components(self) -> MillforgeBaseComponents:
        return self._components

    @property
    def descriptor(self) -> MillforgeBaseRunnerDescriptor:
        return self._descriptor

    @property
    def invocation_evidence(self) -> MillforgeInvocationEvidence:
        return self._invocation_evidence

    async def execute(
        self,
        request: HarnessExecutionRequest,
    ) -> HarnessExecutionResult:
        """Verify and execute one request with fresh mutable invocation state."""
        self._verify_component_composition()
        self._verify_identity_evidence()
        self._verify_request(request)

        executor = _load_invocation_executor()
        return await executor.execute_base_invocation(
            components=self._components,
            request=request,
            model_client=self._services.model_client,
            clock=self._services.clock,
            cancellation_resolver=self._services.cancellation_resolver,
            artifact_writer_factory=self._services.artifact_writer_factory,
        )

    def _verify_identity_evidence(self) -> None:
        if not _has_valid_descriptor_digest(self._descriptor):
            raise MillforgeBaseBindingError("descriptor_hash")
        current_descriptor = describe_millforge_base()
        if (
            self._descriptor != current_descriptor
            or not _descriptor_agrees_with_components(
                self._descriptor, self._components
            )
        ):
            raise MillforgeBaseBindingError("descriptor_composition")
        if not _has_valid_invocation_digest(self._invocation_evidence):
            raise MillforgeBaseBindingError("invocation_evidence_hash")
        try:
            current_evidence = _build_invocation_evidence(
                self._components, self._descriptor
            )
        except (TypeError, ValueError):
            raise MillforgeBaseBindingError("invocation_evidence_composition") from None
        if self._invocation_evidence != current_evidence:
            raise MillforgeBaseBindingError("invocation_evidence_composition")

    def _verify_component_composition(self) -> None:
        plan = self._components.compiled_plan
        metadata = self._components.metadata
        capabilities = self._components.capability_envelope
        profile = self._components.model_profile
        if (
            metadata.harness_id != plan.harness_id
            or metadata.compiled_sha256 != plan.compiled_sha256
            or profile.profile_id != plan.model_profile.profile_id
            or metadata.model_profile_id != profile.profile_id
            or metadata.provider_id != profile.provider_id
            or metadata.model_id != profile.model_id
            or metadata.transport_id != profile.transport_id
            or tuple(grant.capability_id for grant in capabilities.grants)
            != plan.required_capabilities
            or any(grant.constraints is not None for grant in capabilities.grants)
        ):
            raise MillforgeBaseBindingError("backend_composition")

    def _verify_request(self, request: HarnessExecutionRequest) -> None:
        plan = self._components.compiled_plan
        identity = request.compiled_harness.identity
        if (
            identity.harness_id != plan.harness_id
            or identity.harness_version != plan.harness_version
        ):
            raise MillforgeBaseBindingError("harness_identity")
        if request.compiled_harness.expected_hash.digest != plan.compiled_sha256:
            raise MillforgeBaseBindingError("compiled_plan_hash")
        if request.stage.stage_kind_id not in plan.stage_kind_ids:
            raise MillforgeBaseBindingError("stage_kind")
        if request.model_profile.profile_id != plan.model_profile.profile_id:
            raise MillforgeBaseBindingError("model_profile")
        if request.capability_envelope != self._components.capability_envelope:
            raise MillforgeBaseBindingError("capability_envelope")


def create_millforge_base_runner(
    *,
    components: MillforgeBaseComponents,
    services: MillforgeBaseRuntimeServices,
) -> MillforgeBaseRunner:
    """Create the supported runner facade for composed base components."""
    return MillforgeBaseRunner(components=components, services=services)
