"""Focused offline contract tests for the Millforge base runner facade."""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

import millforge.base.runner as runner_module
from millforge import (
    ArtifactRef,
    AssistantMessage,
    BackendTranslationError,
    CancellationRef,
    CapabilityEnvelope,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    CompiledHarnessRef,
    ExecutionResultClass,
    ExecutionStatus,
    HarnessExecutionRequest,
    HarnessExecutionResult,
    MillforgeBaseBindingError,
    MillforgeBaseComponents,
    MillforgeBaseRuntimeServices,
    ModelCompletionResponse,
    ModelProfileRef,
    ModelToolCall,
    ParsedToolArguments,
    RunDirRef,
    RuntimeArtifactWriterFactory,
    StageIdentity,
    TimeoutRef,
    create_millforge_base_runner,
    default_runtime_artifact_writer_factory,
)
from millforge.base import composition
from millforge.testing import FakeModelClient
from millforge.tools.pi_compat.process import PiCompatShellConfig
from tests.conftest import (
    FakeArtifactWriter,
    FakeCancellationResolver,
    FakeClock,
    make_canonical_builder_profile_a,
)


def _components(monkeypatch: pytest.MonkeyPatch, root: Path) -> MillforgeBaseComponents:
    monkeypatch.setattr(
        composition,
        "resolve_pi_compat_shell",
        lambda: PiCompatShellConfig(executable="/test/bin/bash", arguments=("-c",)),
    )
    home = root / "home"
    home.mkdir(exist_ok=True)
    return composition.create_millforge_base_components(
        model_profile=make_canonical_builder_profile_a(),
        cwd=root.resolve(),
        cancellation_resolver=FakeCancellationResolver(),
        prompt_date=datetime.date(2026, 7, 15),
        home_directory=home.resolve(),
    )


def _request(
    components: MillforgeBaseComponents, run_root: Path, *, suffix: str = "one"
) -> HarnessExecutionRequest:
    plan = components.compiled_plan
    return HarnessExecutionRequest(
        request_id=f"request-{suffix}",
        run_id=f"run-{suffix}",
        work_item_id=f"work-{suffix}",
        stage=StageIdentity(
            plane="execution",
            node_id="millforge-base",
            stage_kind_id="millforge_base",
        ),
        compiled_harness=CompiledHarnessRef(
            identity=CompiledHarnessIdentity(
                compiled_plan_id="plan-base-runner",
                harness_id=plan.harness_id,
                harness_version=plan.harness_version,
            ),
            path=run_root / "unused-compiled-plan.json",
            expected_hash=CompiledHarnessHash(
                algorithm="sha256",
                digest=plan.compiled_sha256,
            ),
        ),
        capability_envelope=components.capability_envelope,
        input_artifacts=(
            ArtifactRef(
                artifact_id="input",
                path=Path("millforge/input.txt"),
                content_type="text/plain",
            ),
        ),
        run_directory=RunDirRef(run_id=f"run-{suffix}", path=run_root),
        timeout=TimeoutRef(timeout_seconds=60),
        cancellation=CancellationRef(cancellation_id=f"cancel-{suffix}"),
        secret_refs=(),
        model_profile=ModelProfileRef(profile_id=plan.model_profile.profile_id),
    )


def _prepare_input(request: HarnessExecutionRequest) -> None:
    input_path = request.run_directory.path / request.input_artifacts[0].path
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text("runtime input\n", encoding="utf-8")


def _tool_response(model_id: str, *, terminal: str | None = None):
    if terminal is None:
        name = "read"
        arguments = {"path": "note.txt"}
    else:
        name = {"COMPLETE": "submit", "BLOCKED": "block"}[terminal]
        arguments = {"terminal_result": terminal, "summary": "candidate"}
    return ModelCompletionResponse(
        provider_request_id=f"provider-{name}",
        model_id=model_id,
        message=AssistantMessage(
            content="offline response",
            tool_calls=(
                ModelToolCall(
                    call_id=f"call-{name}",
                    name=name,
                    arguments=ParsedToolArguments(value=arguments),
                ),
            ),
        ),
        finish_reason="tool_calls",
        usage=None,
    )


@pytest.mark.asyncio
async def test_public_facade_runs_tool_then_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "note.txt").write_text("offline proof\n", encoding="utf-8")
    components = _components(monkeypatch, tmp_path)
    plan = components.compiled_plan
    model = FakeModelClient(
        responses=[
            _tool_response(plan.model_profile.profile_id),
            _tool_response(plan.model_profile.profile_id, terminal="COMPLETE"),
        ]
    )
    writers: list[FakeArtifactWriter] = []

    def writer_factory(_run_directory: Path) -> FakeArtifactWriter:
        writer = FakeArtifactWriter()
        writers.append(writer)
        return writer

    runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=model,
            clock=FakeClock(monotonic_value=1.0),
            cancellation_resolver=FakeCancellationResolver(),
            artifact_writer_factory=cast(RuntimeArtifactWriterFactory, writer_factory),
        ),
    )
    request = _request(components, tmp_path)
    _prepare_input(request)

    result = await runner.execute(request)

    assert runner.components is components
    assert result.status is ExecutionStatus.COMPLETED
    assert result.terminal_intent is not None
    assert result.terminal_intent.terminal_result == "COMPLETE"
    assert model.call_count == 2
    assert [record["node_id"] for record in writers[0].tool_trace_calls[0][1]] == [
        "read",
        "submit",
    ]


@pytest.mark.asyncio
async def test_binding_mismatches_precede_all_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components = _components(monkeypatch, tmp_path)
    request = _request(components, tmp_path / "must-not-exist")
    model = FakeModelClient()
    writer_calls: list[Path] = []

    def writer_factory(path: Path) -> FakeArtifactWriter:
        writer_calls.append(path)
        return FakeArtifactWriter()

    runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=model,
            clock=FakeClock(),
            cancellation_resolver=FakeCancellationResolver(),
            artifact_writer_factory=cast(RuntimeArtifactWriterFactory, writer_factory),
        ),
    )
    mismatches = (
        (
            "harness_identity",
            request.model_copy(
                update={
                    "compiled_harness": request.compiled_harness.model_copy(
                        update={
                            "identity": request.compiled_harness.identity.model_copy(
                                update={"harness_version": 999}
                            )
                        }
                    )
                }
            ),
        ),
        (
            "compiled_plan_hash",
            request.model_copy(
                update={
                    "compiled_harness": request.compiled_harness.model_copy(
                        update={
                            "expected_hash": CompiledHarnessHash(
                                algorithm="sha256", digest="f" * 64
                            )
                        }
                    )
                }
            ),
        ),
        (
            "stage_kind",
            request.model_copy(
                update={
                    "stage": request.stage.model_copy(update={"stage_kind_id": "other"})
                }
            ),
        ),
        (
            "model_profile",
            request.model_copy(
                update={"model_profile": ModelProfileRef(profile_id="other")}
            ),
        ),
        (
            "capability_envelope",
            request.model_copy(
                update={"capability_envelope": CapabilityEnvelope(grants=())}
            ),
        ),
    )

    for reason, mismatched_request in mismatches:
        with pytest.raises(MillforgeBaseBindingError) as caught:
            await runner.execute(mismatched_request)
        assert caught.value.reason == reason
        assert len(str(caught.value).encode("utf-8")) <= 2048

    assert writer_calls == []
    assert not request.run_directory.path.exists()
    model.assert_not_called()


@pytest.mark.asyncio
async def test_each_sequential_and_concurrent_call_gets_fresh_invocation_objects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components = _components(monkeypatch, tmp_path)
    backends: list[object] = []
    tool_executors: list[object] = []
    runtimes: list[object] = []
    lazy_writers: list[object] = []
    writers: list[FakeArtifactWriter] = []
    active = 0
    both_active = asyncio.Event()
    concurrent_mode = False
    invocation_executor = runner_module._load_invocation_executor()

    class RecordingBackend:
        def __init__(self, **kwargs: object) -> None:
            backends.append(self)
            tool_executors.append(kwargs["tool_executor"])

    class RecordingRuntime:
        def __init__(self, **kwargs: object) -> None:
            runtimes.append(self)
            lazy_writers.append(kwargs["artifact_writer"])

        async def execute(self, request: HarnessExecutionRequest) -> Any:
            nonlocal active
            if not concurrent_mode:
                return request
            active += 1
            if active == 2:
                both_active.set()
            await asyncio.wait_for(both_active.wait(), timeout=1)
            active -= 1
            return request

    def writer_factory(_path: Path) -> FakeArtifactWriter:
        writer = FakeArtifactWriter()
        writers.append(writer)
        return writer

    monkeypatch.setattr(invocation_executor, "ForgeGuardrailBackend", RecordingBackend)
    monkeypatch.setattr(invocation_executor, "DefaultHarnessRuntime", RecordingRuntime)
    runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=FakeModelClient(),
            clock=FakeClock(),
            cancellation_resolver=FakeCancellationResolver(),
            artifact_writer_factory=cast(RuntimeArtifactWriterFactory, writer_factory),
        ),
    )
    first = _request(components, tmp_path / "first", suffix="first")
    second = _request(components, tmp_path / "second", suffix="second")

    sequential = [await runner.execute(first), await runner.execute(second)]
    concurrent_mode = True
    results = await asyncio.gather(runner.execute(first), runner.execute(second))

    assert [result.request_id for result in sequential] == [
        "request-first",
        "request-second",
    ]
    assert [result.request_id for result in results] == [
        "request-first",
        "request-second",
    ]
    assert len({id(item) for item in backends}) == 4
    assert len({id(item) for item in runtimes}) == 4
    assert len({id(item) for item in lazy_writers}) == 4
    assert len({id(item) for item in tool_executors}) == 4
    assert all(item is not components.tool_executor for item in tool_executors)
    assert writers == []


@pytest.mark.asyncio
async def test_real_executor_trace_state_is_isolated_sequentially_and_concurrently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "note.txt").write_text("offline proof\n", encoding="utf-8")
    components = _components(monkeypatch, tmp_path)
    model_id = components.compiled_plan.model_profile.profile_id

    def writer_factory_for(writers: list[FakeArtifactWriter]):
        def factory(_path: Path) -> FakeArtifactWriter:
            writer = FakeArtifactWriter()
            writers.append(writer)
            return writer

        return cast(RuntimeArtifactWriterFactory, factory)

    sequential_writers: list[FakeArtifactWriter] = []
    sequential_runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=FakeModelClient(
                responses=[
                    _tool_response(model_id),
                    _tool_response(model_id, terminal="COMPLETE"),
                    _tool_response(model_id),
                    _tool_response(model_id, terminal="COMPLETE"),
                ]
            ),
            clock=FakeClock(monotonic_value=1.0),
            cancellation_resolver=FakeCancellationResolver(),
            artifact_writer_factory=writer_factory_for(sequential_writers),
        ),
    )
    sequential_requests = (
        _request(components, tmp_path / "sequential-one", suffix="sequential-one"),
        _request(components, tmp_path / "sequential-two", suffix="sequential-two"),
    )
    for request in sequential_requests:
        _prepare_input(request)

    sequential_results = [
        await sequential_runner.execute(request) for request in sequential_requests
    ]

    class ConcurrentModelClient:
        def __init__(self) -> None:
            self._calls_by_task: dict[asyncio.Task[Any], int] = {}
            self._first_call_count = 0
            self._both_started = asyncio.Event()

        async def complete(self, _request: object) -> ModelCompletionResponse:
            task = asyncio.current_task()
            assert task is not None
            call_index = self._calls_by_task.get(task, 0)
            self._calls_by_task[task] = call_index + 1
            if call_index == 0:
                self._first_call_count += 1
                if self._first_call_count == 2:
                    self._both_started.set()
                await asyncio.wait_for(self._both_started.wait(), timeout=5)
                return _tool_response(model_id)
            return _tool_response(model_id, terminal="COMPLETE")

    concurrent_writers: list[FakeArtifactWriter] = []
    concurrent_runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=ConcurrentModelClient(),
            clock=FakeClock(monotonic_value=1.0),
            cancellation_resolver=FakeCancellationResolver(),
            artifact_writer_factory=writer_factory_for(concurrent_writers),
        ),
    )
    concurrent_requests = (
        _request(components, tmp_path / "concurrent-one", suffix="concurrent-one"),
        _request(components, tmp_path / "concurrent-two", suffix="concurrent-two"),
    )
    for request in concurrent_requests:
        _prepare_input(request)

    concurrent_results = await asyncio.gather(
        *(concurrent_runner.execute(request) for request in concurrent_requests)
    )

    assert all(
        result.status is ExecutionStatus.COMPLETED for result in sequential_results
    )
    assert all(
        result.status is ExecutionStatus.COMPLETED for result in concurrent_results
    )
    for writer in (*sequential_writers, *concurrent_writers):
        records = writer.tool_trace_calls[0][1]
        assert [record["node_id"] for record in records] == ["read", "submit"]
        assert [record["sequence"] for record in records] == [1, 2]
    assert components.tool_executor.trace_records == ()


@pytest.mark.asyncio
async def test_blocked_terminal_is_returned_without_external_routing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components = _components(monkeypatch, tmp_path)
    plan = components.compiled_plan
    routing_actions: list[str] = []
    runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=FakeModelClient(
                responses=[
                    _tool_response(plan.model_profile.profile_id, terminal="BLOCKED")
                ]
            ),
            clock=FakeClock(monotonic_value=1.0),
            cancellation_resolver=FakeCancellationResolver(),
            artifact_writer_factory=cast(
                RuntimeArtifactWriterFactory,
                lambda _path: FakeArtifactWriter(),
            ),
        ),
    )
    request = _request(components, tmp_path)
    _prepare_input(request)

    result = await runner.execute(request)

    assert result.terminal_intent is not None
    assert result.terminal_intent.terminal_result == "BLOCKED"
    assert routing_actions == []


@pytest.mark.asyncio
async def test_default_writer_factory_is_scoped_below_run_root(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    writer = default_runtime_artifact_writer_factory(run_root)
    await writer.write_metrics(
        ArtifactRef(artifact_id="metrics", path=Path("millforge/metrics.json")),
        {
            "schema_version": "1.0",
            "request_id": "request-writer",
            "run_id": "run-writer",
            "status": "completed",
            "usage": None,
        },
    )

    written = tuple(path for path in tmp_path.rglob("*") if path.is_file())
    assert written == (run_root / "millforge" / "metrics.json",)
    assert all(path.resolve().is_relative_to(run_root.resolve()) for path in written)
    assert tuple(tmp_path.iterdir()) == (run_root,)


@pytest.mark.asyncio
async def test_unusable_run_root_returns_typed_runtime_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components = _components(monkeypatch, tmp_path)
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("blocked\n", encoding="utf-8")
    request = _request(components, blocking_file / "run")
    model = FakeModelClient()
    runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=model,
            clock=FakeClock(monotonic_value=1.0),
            cancellation_resolver=FakeCancellationResolver(),
        ),
    )

    result = await runner.execute(request)

    assert result.status is ExecutionStatus.FAILED
    assert result.result_class is ExecutionResultClass.INTERNAL_FAILURE
    assert result.diagnostic is not None
    assert result.diagnostic.origin == "infrastructure_failure"
    assert result.artifact_refs == ()
    model.assert_not_called()


@pytest.mark.asyncio
async def test_writer_factory_failure_returns_typed_artifact_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components = _components(monkeypatch, tmp_path)
    model_id = components.compiled_plan.model_profile.profile_id
    model = FakeModelClient(responses=[_tool_response(model_id, terminal="COMPLETE")])
    factory_calls: list[Path] = []

    def failing_factory(path: Path) -> FakeArtifactWriter:
        factory_calls.append(path)
        raise OSError("writer construction failed")

    runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=model,
            clock=FakeClock(monotonic_value=1.0),
            cancellation_resolver=FakeCancellationResolver(),
            artifact_writer_factory=cast(
                RuntimeArtifactWriterFactory,
                failing_factory,
            ),
        ),
    )
    request = _request(components, tmp_path)
    _prepare_input(request)

    result = await runner.execute(request)

    assert result.status is ExecutionStatus.FAILED
    assert result.result_class is ExecutionResultClass.ARTIFACT_FINALIZATION_FAILED
    assert result.diagnostic is not None
    assert result.diagnostic.origin == "artifact_write_failure"
    assert factory_calls == [tmp_path]


@pytest.mark.asyncio
async def test_private_forge_error_is_translated_to_public_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components = _components(monkeypatch, tmp_path)
    invocation_executor = cast(Any, runner_module._load_invocation_executor())

    class FailingRuntime:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def execute(
            self, _request: HarnessExecutionRequest
        ) -> HarnessExecutionResult:
            raise invocation_executor.ForgeError("private detail")

    monkeypatch.setattr(invocation_executor, "DefaultHarnessRuntime", FailingRuntime)
    runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=FakeModelClient(),
            clock=FakeClock(),
            cancellation_resolver=FakeCancellationResolver(),
            artifact_writer_factory=cast(
                RuntimeArtifactWriterFactory,
                lambda _path: FakeArtifactWriter(),
            ),
        ),
    )

    with pytest.raises(BackendTranslationError, match="Private Forge backend failed"):
        await runner.execute(_request(components, tmp_path))


@pytest.mark.asyncio
async def test_constructor_time_private_forge_error_is_translated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components = _components(monkeypatch, tmp_path)
    invocation_executor = cast(Any, runner_module._load_invocation_executor())
    writer_calls: list[Path] = []

    class FailingBackend:
        def __init__(self, **_kwargs: object) -> None:
            raise invocation_executor.ForgeError("constructor detail")

    def writer_factory(path: Path) -> FakeArtifactWriter:
        writer_calls.append(path)
        return FakeArtifactWriter()

    monkeypatch.setattr(invocation_executor, "ForgeGuardrailBackend", FailingBackend)
    runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=FakeModelClient(),
            clock=FakeClock(),
            cancellation_resolver=FakeCancellationResolver(),
            artifact_writer_factory=cast(RuntimeArtifactWriterFactory, writer_factory),
        ),
    )

    with pytest.raises(BackendTranslationError) as caught:
        await runner.execute(_request(components, tmp_path))

    assert str(caught.value) == "Private Forge backend failed"
    assert caught.value.__cause__ is None
    assert writer_calls == []


def test_inconsistent_components_are_rejected_at_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components = _components(monkeypatch, tmp_path)
    inconsistent = replace(
        components,
        metadata=components.metadata.model_copy(update={"compiled_sha256": "f" * 64}),
    )

    with pytest.raises(MillforgeBaseBindingError) as caught:
        create_millforge_base_runner(
            components=inconsistent,
            services=MillforgeBaseRuntimeServices(
                model_client=FakeModelClient(),
                clock=FakeClock(),
                cancellation_resolver=FakeCancellationResolver(),
            ),
        )
    assert caught.value.reason == "backend_composition"
