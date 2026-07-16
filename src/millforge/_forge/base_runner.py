"""Private Forge graph assembly for the public Millforge base runner facade."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from millforge._forge.adapter import ForgeContextFactory, ForgeGuardrailBackend
from millforge._forge.errors import ForgeError
from millforge.base.composition import MillforgeBaseComponents
from millforge.compiled_plan import CompiledHarnessPlan
from millforge.contracts import (
    ArtifactRef,
    CompiledHarnessRef,
    HarnessExecutionRequest,
    HarnessExecutionResult,
)
from millforge.exceptions import BackendTranslationError
from millforge.protocols import (
    CancellationResolver,
    ModelClient,
    RuntimeArtifactWriter,
    RuntimeClock,
)
from millforge.runtime import DefaultHarnessRuntime


@dataclass(frozen=True, slots=True)
class _PinnedCompiledHarnessLoader:
    plan: CompiledHarnessPlan

    async def load(self, ref: CompiledHarnessRef) -> CompiledHarnessPlan:
        del ref
        return self.plan


class _LazyRuntimeArtifactWriter:
    """Defer writer construction until runtime run-directory preparation succeeds."""

    __slots__ = ("_error", "_factory", "_run_directory", "_writer")

    def __init__(
        self,
        *,
        factory: Callable[[Path], RuntimeArtifactWriter],
        run_directory: Path,
    ) -> None:
        self._factory = factory
        self._run_directory = run_directory
        self._writer: RuntimeArtifactWriter | None = None
        self._error: Exception | None = None

    def _resolve(self) -> RuntimeArtifactWriter:
        if self._error is not None:
            raise self._error
        if self._writer is None:
            try:
                self._writer = self._factory(self._run_directory)
            except Exception as exc:
                self._error = exc
                raise
        return self._writer

    async def write_terminal_result(self, ref: ArtifactRef, data: object) -> None:
        await self._resolve().write_terminal_result(ref, data)

    async def write_execution_summary(self, ref: ArtifactRef, data: object) -> None:
        await self._resolve().write_execution_summary(ref, data)

    async def write_events(self, ref: ArtifactRef, data: object) -> None:
        await self._resolve().write_events(ref, data)

    async def write_tool_trace(self, ref: ArtifactRef, data: object) -> None:
        await self._resolve().write_tool_trace(ref, data)

    async def write_metrics(self, ref: ArtifactRef, data: object) -> None:
        await self._resolve().write_metrics(ref, data)

    async def write_artifact_manifest(self, ref: ArtifactRef, data: object) -> None:
        await self._resolve().write_artifact_manifest(ref, data)

    async def write_diagnostic(self, ref: ArtifactRef, data: object) -> None:
        await self._resolve().write_diagnostic(ref, data)


async def execute_base_invocation(
    *,
    components: MillforgeBaseComponents,
    request: HarnessExecutionRequest,
    model_client: ModelClient,
    clock: RuntimeClock,
    cancellation_resolver: CancellationResolver,
    artifact_writer_factory: Callable[[Path], RuntimeArtifactWriter],
) -> HarnessExecutionResult:
    """Build and execute one fresh private adapter graph."""
    try:
        loader = _PinnedCompiledHarnessLoader(components.compiled_plan)
        writer = _LazyRuntimeArtifactWriter(
            factory=artifact_writer_factory,
            run_directory=request.run_directory.path,
        )
        tool_executor = components.tool_executor.fork_for_invocation()
        backend = ForgeGuardrailBackend(
            model_client=model_client,
            tool_executor=tool_executor,
            plan_loader=loader,
            context_factory=ForgeContextFactory(),
            clock=clock,
            cancellation_resolver=cancellation_resolver,
        )
        runtime = DefaultHarnessRuntime(
            backend=backend,
            plan_loader=loader,
            artifact_writer=writer,
            clock=clock,
            cancellation_resolver=cancellation_resolver,
        )
        return await runtime.execute(request)
    except ForgeError:
        raise BackendTranslationError("Private Forge backend failed") from None
