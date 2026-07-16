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
]

_BINDING_MESSAGES: dict[_BindingReason, str] = {
    "harness_identity": "Execution request harness identity does not match millforge-base components",
    "compiled_plan_hash": "Execution request plan hash does not match millforge-base components",
    "stage_kind": "Execution request stage kind is not admitted by millforge-base components",
    "model_profile": "Execution request model profile does not match millforge-base components",
    "capability_envelope": "Execution request capabilities do not match millforge-base components",
    "backend_composition": "Millforge-base components are not internally consistent",
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

    __slots__ = ("_components", "_services")

    def __init__(
        self,
        *,
        components: MillforgeBaseComponents,
        services: MillforgeBaseRuntimeServices,
    ) -> None:
        self._components = components
        self._services = services
        self._verify_component_composition()

    @property
    def components(self) -> MillforgeBaseComponents:
        return self._components

    async def execute(
        self,
        request: HarnessExecutionRequest,
    ) -> HarnessExecutionResult:
        """Verify and execute one request with fresh mutable invocation state."""
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

    def _verify_component_composition(self) -> None:
        plan = self._components.compiled_plan
        metadata = self._components.metadata
        capabilities = self._components.capability_envelope
        if (
            metadata.harness_id != plan.harness_id
            or metadata.compiled_sha256 != plan.compiled_sha256
            or metadata.model_profile_id != plan.model_profile.profile_id
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
