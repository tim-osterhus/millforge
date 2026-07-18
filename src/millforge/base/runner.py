"""Stable in-process invocation facade for composed Millforge base components."""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Literal, Protocol, Self, cast

from pydantic import ValidationError

from millforge.artifacts import RuntimeArtifactWriter as _AtomicRuntimeArtifactWriter
from millforge.contracts import (
    HarnessExecutionRequest,
    HarnessExecutionResult,
    ModelCapabilityRequirements,
    SecretRef,
)
from millforge.exceptions import (
    MillforgeBaseClosedError,
    MillforgeConfigError,
    MillforgeError,
)
from millforge.model_backend import (
    CapabilitySupport,
    DefaultModelClient,
    ModelBackendConfigError,
    OpenAIChatCompletionsTransport,
    OpenAICompatibleTimeouts,
    ResolvedModelProfile,
    SecretResolutionError,
    SecretResolver,
    StaticModelProfileResolver,
    UnsupportedModelCapabilityError,
)
from millforge.protocols import (
    AsyncHttpTransport,
    CancellationResolver,
    ModelClient,
    RuntimeArtifactWriter,
    RuntimeClock,
)
from .composition import MillforgeBaseComponents, create_millforge_base_components
from .identity import (
    MillforgeBaseRunnerDescriptor,
    MillforgeInvocationEvidence,
    _build_invocation_evidence,
    _descriptor_agrees_with_components,
    _has_valid_descriptor_digest,
    _has_valid_invocation_digest,
    _MILLFORGE_BASE_STAGE_IDENTITY,
    describe_millforge_base,
)
from .platform import _require_supported_platform
from .options import MillforgeBaseOptions

__all__ = [
    "RuntimeArtifactWriterFactory",
    "default_runtime_artifact_writer_factory",
    "MillforgeBaseRuntimeServices",
    "MillforgeBaseBindingError",
    "MillforgeBaseRunner",
    "create_millforge_base_runner",
    "MillforgeBaseLiveRunner",
    "create_millforge_base_live_runner",
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
    "stage_kind": "Execution request provider-local stage identity is not admitted by millforge-base components",
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
    """Execute requests against one immutable Millforge base composition.

    Requests must use the provider-local stage identity pinned by
    ``millforge-base``.  Caller workflow identities and routing authority remain
    outside this facade.
    """

    __slots__ = ("_components", "_descriptor", "_services")

    def __init__(
        self,
        *,
        components: MillforgeBaseComponents,
        services: MillforgeBaseRuntimeServices,
    ) -> None:
        _require_supported_platform()
        self._components = components
        self._services = services
        self._descriptor = describe_millforge_base()
        self._verify_component_composition()
        self._verify_identity_evidence()

    @property
    def components(self) -> MillforgeBaseComponents:
        return self._components

    @property
    def descriptor(self) -> MillforgeBaseRunnerDescriptor:
        return self._descriptor

    def invocation_evidence_for(
        self,
        request: HarnessExecutionRequest,
    ) -> MillforgeInvocationEvidence:
        """Return immutable request-local evidence for one admitted request.

        The caller's correlation IDs are included verbatim for correlation only;
        they do not select any workflow behavior or terminal authority.
        """
        self._verify_component_composition()
        self._verify_identity_evidence()
        self._verify_request(request)
        return self._build_request_invocation_evidence(request)

    async def execute(
        self,
        request: HarnessExecutionRequest,
    ) -> HarnessExecutionResult:
        """Admit the pinned provider identity, then execute fresh invocation state."""
        _require_supported_platform()
        self._verify_component_composition()
        self._verify_identity_evidence()
        self._verify_request(request)
        self._build_request_invocation_evidence(request)

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

    def _build_request_invocation_evidence(
        self,
        request: HarnessExecutionRequest,
    ) -> MillforgeInvocationEvidence:
        try:
            evidence = _build_invocation_evidence(
                self._components,
                self._descriptor,
                request_id=request.request_id,
                run_id=request.run_id,
                selected_output=request.selected_output,
            )
        except (TypeError, ValueError):
            raise MillforgeBaseBindingError("invocation_evidence_composition") from None
        if not _has_valid_invocation_digest(evidence):
            raise MillforgeBaseBindingError("invocation_evidence_hash")
        return evidence

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
        if (
            request.stage != _MILLFORGE_BASE_STAGE_IDENTITY
            or request.stage.stage_kind_id not in plan.stage_kind_ids
        ):
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
    """Create the facade that admits only the pinned provider-local base stage."""
    _require_supported_platform()
    return MillforgeBaseRunner(components=components, services=services)


class MillforgeBaseLiveRunner:
    """Owned live OpenAI-compatible runner with close-once async lifecycle.

    The composition owns its model client and the HTTP client created behind it.
    Caller-provided resolvers, clock, artifact-writer factory, and injected
    ``httpx.AsyncBaseTransport`` remain caller-owned and are never closed here.
    """

    __slots__ = ("_model_client", "_runner", "_closed", "_close_task")

    def __init__(
        self,
        *,
        runner: MillforgeBaseRunner,
        model_client: DefaultModelClient,
    ) -> None:
        self._runner = runner
        self._model_client = model_client
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def components(self) -> MillforgeBaseComponents:
        """Return the immutable composition used by this live runner."""
        return self._runner.components

    @property
    def descriptor(self) -> MillforgeBaseRunnerDescriptor:
        """Return the stable public base-runner descriptor."""
        return self._runner.descriptor

    @property
    def is_closed(self) -> bool:
        """Return whether close has begun."""
        return self._closed

    async def __aenter__(self) -> Self:
        self._require_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close factory-owned resources exactly once."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(
                self._model_client.aclose(),
                name="millforge-base-live-close",
            )
        await asyncio.shield(self._close_task)

    def invocation_evidence_for(
        self,
        request: HarnessExecutionRequest,
    ) -> MillforgeInvocationEvidence:
        """Return request-local evidence while this live runner is open."""
        self._require_open()
        return self._runner.invocation_evidence_for(request)

    async def execute(
        self,
        request: HarnessExecutionRequest,
    ) -> HarnessExecutionResult:
        """Execute one request, rejecting use after close before any work."""
        self._require_open()
        return await self._runner.execute(request)

    def _require_open(self) -> None:
        if self._closed:
            raise MillforgeBaseClosedError()


def _validate_live_profile(
    *,
    profile_id: str,
    model_profile: ResolvedModelProfile,
    secret_ref: SecretRef,
    secret_resolver: SecretResolver,
) -> ResolvedModelProfile:
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ModelBackendConfigError("profile_id must be a non-empty string")
    try:
        admitted_profile = ResolvedModelProfile.model_validate(
            model_profile.model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise ModelBackendConfigError("resolved model profile is invalid") from None
    if admitted_profile.profile_id != profile_id:
        raise ModelBackendConfigError("logical and resolved profile IDs must match")

    required = ModelCapabilityRequirements()
    unsupported = tuple(
        name
        for name in ("tool_calls", "system_messages", "tool_result_messages")
        if getattr(required, name)
        and admitted_profile.capabilities.state_for(name)
        is not CapabilitySupport.SUPPORTED
    )
    if unsupported:
        raise UnsupportedModelCapabilityError(
            "millforge-base requires supported model capabilities: "
            + ", ".join(unsupported)
        )

    configured_secret = admitted_profile.authentication.secret_ref
    if configured_secret is None or configured_secret != secret_ref:
        raise SecretResolutionError(
            "admitted secret does not match resolved profile authentication"
        )
    if not isinstance(secret_resolver, SecretResolver):
        raise SecretResolutionError("secret_resolver does not implement SecretResolver")
    return admitted_profile


async def create_millforge_base_live_runner(
    *,
    profile_id: str,
    model_profile: ResolvedModelProfile,
    secret_ref: SecretRef,
    secret_resolver: SecretResolver,
    cwd: Path,
    clock: RuntimeClock,
    cancellation_resolver: CancellationResolver,
    artifact_writer_factory: RuntimeArtifactWriterFactory = (
        default_runtime_artifact_writer_factory
    ),
    timeouts: OpenAICompatibleTimeouts,
    http_transport: AsyncHttpTransport | None = None,
    options: MillforgeBaseOptions | None = None,
    prompt_date: datetime.date | None = None,
    home_directory: Path | None = None,
) -> MillforgeBaseLiveRunner:
    """Build one live-capable base runner without network or provider probing.

    The async construction boundary exists so a factory-owned HTTP client can be
    closed deterministically if a later local construction step fails. Exactly
    one logical profile and the matching immutable resolved profile are admitted.
    """
    _require_supported_platform()
    if not isinstance(timeouts, OpenAICompatibleTimeouts):
        raise ModelBackendConfigError("timeouts must be OpenAICompatibleTimeouts")
    if not isinstance(clock, RuntimeClock):
        raise ModelBackendConfigError("clock does not implement RuntimeClock")
    if not isinstance(cancellation_resolver, CancellationResolver):
        raise ModelBackendConfigError(
            "cancellation_resolver does not implement CancellationResolver"
        )
    if not callable(artifact_writer_factory):
        raise ModelBackendConfigError("artifact_writer_factory must be callable")
    admitted_profile = _validate_live_profile(
        profile_id=profile_id,
        model_profile=model_profile,
        secret_ref=secret_ref,
        secret_resolver=secret_resolver,
    )

    try:
        components = create_millforge_base_components(
            model_profile=admitted_profile,
            cwd=cwd,
            cancellation_resolver=cancellation_resolver,
            options=options,
            prompt_date=prompt_date,
            home_directory=home_directory,
        )
    except MillforgeError:
        raise
    except Exception:
        raise ModelBackendConfigError("millforge-base composition is invalid") from None

    transport = OpenAIChatCompletionsTransport(
        http_transport=http_transport,
        timeouts=timeouts,
    )
    model_client: DefaultModelClient | None = None

    async def close_partial_composition() -> None:
        owned_resource = transport if model_client is None else model_client
        await asyncio.shield(owned_resource.aclose())

    try:
        model_client = DefaultModelClient(
            profile_resolver=StaticModelProfileResolver({profile_id: admitted_profile}),
            secret_resolver=secret_resolver,
            transport=transport,
            cancellation_resolver=cancellation_resolver,
            clock=clock,
            local_timeout_seconds=timeouts.local_total_seconds,
        )
        runner = create_millforge_base_runner(
            components=components,
            services=MillforgeBaseRuntimeServices(
                model_client=model_client,
                clock=clock,
                cancellation_resolver=cancellation_resolver,
                artifact_writer_factory=artifact_writer_factory,
            ),
        )
        return MillforgeBaseLiveRunner(runner=runner, model_client=model_client)
    except asyncio.CancelledError:
        await close_partial_composition()
        raise
    except MillforgeError:
        await close_partial_composition()
        raise
    except Exception:
        await close_partial_composition()
        raise ModelBackendConfigError("live runner construction failed") from None
    except BaseException:
        await close_partial_composition()
        raise
