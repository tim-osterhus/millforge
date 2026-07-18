"""Focused offline contract tests for the Millforge base runner facade."""

from __future__ import annotations

import asyncio
import datetime
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

import millforge.base.runner as runner_module
import millforge.model_backend as model_backend_module
from millforge import (
    ArtifactRef,
    AssistantMessage,
    AsyncHttpTransport,
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
    HarnessTaskInput,
    InvalidToolArguments,
    MillforgeBaseBindingError,
    MillforgeBaseClosedError,
    MillforgeBaseComponents,
    MillforgeBaseRunnerDescriptor,
    MillforgeBaseRuntimeServices,
    MillforgeInvocationEvidence,
    ModelBackendConfigError,
    ModelCompletionResponse,
    ModelCompletionRequest,
    ModelProfileRef,
    ModelToolCall,
    OpenAICompatibleTimeouts,
    ParsedToolArguments,
    RunDirRef,
    RuntimeArtifactWriterFactory,
    ResolvedSecret,
    SecretRef,
    SecretResolutionError,
    SelectedOutputAbsent,
    SelectedOutputPresent,
    SelectedOutputRequirement,
    StageIdentity,
    TimeoutRef,
    create_millforge_base_live_runner,
    create_millforge_base_runner,
    default_runtime_artifact_writer_factory,
)
from millforge.contracts import admit_selected_output
from millforge.base import composition
from millforge.testing import FakeModelClient
from millforge.tools.pi_compat.process import PiCompatShellConfig
from tests.conftest import (
    FakeArtifactWriter,
    FakeCancellationResolver,
    FakeClock,
    make_canonical_builder_profile_a,
)


def _components(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    legal_terminal_results: tuple[str, ...] = ("BLOCKED", "COMPLETE", "REJECTED"),
) -> MillforgeBaseComponents:
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
        legal_terminal_results=legal_terminal_results,
        prompt_date=datetime.date(2026, 7, 15),
        home_directory=home.resolve(),
    )


def _request(
    components: MillforgeBaseComponents,
    run_root: Path,
    *,
    suffix: str = "one",
    request_id: str | None = None,
    run_id: str | None = None,
) -> HarnessExecutionRequest:
    plan = components.compiled_plan
    request_id = request_id or f"request-{suffix}"
    run_id = run_id or f"run-{suffix}"
    return HarnessExecutionRequest(
        request_id=request_id,
        run_id=run_id,
        work_item_id=f"work-{suffix}",
        task=HarnessTaskInput(instruction="Read note.txt and complete the task."),
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
        input_artifacts=(),
        run_directory=RunDirRef(run_id=run_id, path=run_root),
        timeout=TimeoutRef(timeout_seconds=60),
        cancellation=CancellationRef(cancellation_id=f"cancel-{suffix}"),
        secret_refs=(),
        model_profile=ModelProfileRef(profile_id=plan.model_profile.profile_id),
    )


def _prepare_input(request: HarnessExecutionRequest) -> None:
    assert request.input_artifacts == ()


class _TaskGatedModelClient(FakeModelClient):
    def __init__(
        self,
        *,
        expected_instruction: str,
        responses: list[ModelCompletionResponse],
    ) -> None:
        super().__init__(responses=responses)
        self._expected_instruction = expected_instruction

    async def complete(
        self, request: ModelCompletionRequest
    ) -> ModelCompletionResponse:
        if (
            len(request.messages) < 2
            or request.messages[0].role != "system"
            or request.messages[1].role != "user"
            or not request.messages[1].content.startswith(self._expected_instruction)
        ):
            raise AssertionError(
                "exact task instruction was not the primary user input"
            )
        return await super().complete(request)


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


_CANDIDATE_OMITTED = object()


def _terminal_response(
    model_id: str,
    *,
    candidate: Any = _CANDIDATE_OMITTED,
    terminal: str = "COMPLETE",
    call_suffix: str = "selected",
    content: str | None = "offline response",
) -> ModelCompletionResponse:
    name = {"COMPLETE": "submit", "BLOCKED": "block", "REJECTED": "reject"}[terminal]
    arguments: dict[str, Any] = {
        "terminal_result": terminal,
        "summary": "candidate summary",
    }
    if candidate is not _CANDIDATE_OMITTED:
        arguments["candidate"] = candidate
    return ModelCompletionResponse(
        provider_request_id=f"provider-{name}-{call_suffix}",
        model_id=model_id,
        message=AssistantMessage(
            content=content,
            tool_calls=(
                ModelToolCall(
                    call_id=f"call-{name}-{call_suffix}",
                    name=name,
                    arguments=ParsedToolArguments(value=arguments),
                ),
            ),
        ),
        finish_reason="tool_calls",
        usage=None,
    )


def _configured_terminal_response(
    model_id: str,
    *,
    terminal_result: str,
    candidate: Any = _CANDIDATE_OMITTED,
) -> ModelCompletionResponse:
    arguments: dict[str, Any] = {
        "terminal_result": terminal_result,
        "summary": f"{terminal_result} summary",
    }
    if candidate is not _CANDIDATE_OMITTED:
        arguments["candidate"] = candidate
    tool_name = f"terminal_{terminal_result.lower()}"
    return ModelCompletionResponse(
        provider_request_id=f"provider-{tool_name}",
        model_id=model_id,
        message=AssistantMessage(
            content="configured terminal",
            tool_calls=(
                ModelToolCall(
                    call_id=f"call-{tool_name}",
                    name=tool_name,
                    arguments=ParsedToolArguments(value=arguments),
                ),
            ),
        ),
        finish_reason="tool_calls",
        usage=None,
    )


def _invalid_terminal_response(
    model_id: str,
    *,
    raw: str = "{not-json",
    call_suffix: str,
    content: str | None = None,
) -> ModelCompletionResponse:
    return ModelCompletionResponse(
        provider_request_id=f"provider-submit-{call_suffix}",
        model_id=model_id,
        message=AssistantMessage(
            content=content,
            tool_calls=(
                ModelToolCall(
                    call_id=f"call-submit-{call_suffix}",
                    name="submit",
                    arguments=InvalidToolArguments(
                        raw=raw,
                        error_code="malformed_json",
                    ),
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
    request = _request(components, tmp_path)
    model = _TaskGatedModelClient(
        expected_instruction=request.task.instruction,
        responses=[
            _tool_response(plan.model_profile.profile_id),
            _tool_response(plan.model_profile.profile_id, terminal="COMPLETE"),
        ],
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
    _prepare_input(request)

    result = await runner.execute(request)

    assert runner.components is components
    assert result.status is ExecutionStatus.COMPLETED
    assert result.terminal_intent is not None
    assert result.terminal_intent.terminal_result == "COMPLETE"
    assert request.input_artifacts == ()
    assert model.call_count == 2
    assert [record["node_id"] for record in writers[0].tool_trace_calls[0][1]] == [
        "read",
        "submit",
    ]


@pytest.mark.asyncio
async def test_selected_output_null_content_corrects_then_admits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components = _components(monkeypatch, tmp_path)
    plan = components.compiled_plan
    requirement = SelectedOutputRequirement(
        required=True,
        json_schema={
            "type": "object",
            "properties": {"answer": {"type": "string", "minLength": 2}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    request = _request(components, tmp_path).model_copy(
        update={"selected_output": requirement}
    )
    model = FakeModelClient(
        responses=[
            _terminal_response(
                plan.model_profile.profile_id,
                candidate={"answer": 1},
                call_suffix="invalid",
                content=None,
            ),
            _terminal_response(
                plan.model_profile.profile_id,
                candidate={"answer": "ok"},
                call_suffix="valid",
                content=None,
            ),
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

    result = await runner.execute(request)

    assert result.status is ExecutionStatus.COMPLETED
    assert model.call_count == 2
    submit_tools = [
        tool
        for tool in model.requests[0].tools
        if tool.name in {"submit", "block", "reject"}
    ]
    assert len(submit_tools) == 3
    for tool in submit_tools:
        assert tool.input_schema["properties"]["candidate"] == requirement.json_schema
        assert "candidate" in tool.input_schema["required"]
    assert "candidate" not in runner.descriptor.model_dump_json()
    assert result.selected_output == SelectedOutputPresent(value={"answer": "ok"})
    assert result.selected_output_schema_sha256 == requirement.schema_sha256
    assert result.terminal_intent is not None
    assert result.terminal_intent.selected_output == result.selected_output
    event_types = [event["event_type"] for event in writers[0].events_calls[0][1]]
    assert "terminal_intent_rejected" in event_types
    assert "correction_issued" in event_types
    terminal_artifact = writers[0].terminal_result_calls[0][1]
    assert "candidate" not in terminal_artifact
    assert "selected_output" not in terminal_artifact


@pytest.mark.asyncio
async def test_same_runner_keeps_optional_absence_distinct_from_null_and_schema_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components = _components(monkeypatch, tmp_path)
    profile_id = components.compiled_plan.model_profile.profile_id
    optional_null = SelectedOutputRequirement(
        required=False,
        json_schema={"type": "null"},
    )
    required_array = SelectedOutputRequirement(
        required=True,
        json_schema={
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 1,
            "maxItems": 3,
        },
    )
    model = FakeModelClient(
        responses=[
            _terminal_response(profile_id, call_suffix="absent"),
            _terminal_response(profile_id, candidate=None, call_suffix="null"),
            _terminal_response(profile_id, candidate=[1, 2], call_suffix="array"),
        ]
    )
    runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=model,
            clock=FakeClock(monotonic_value=1.0),
            cancellation_resolver=FakeCancellationResolver(),
            artifact_writer_factory=cast(
                RuntimeArtifactWriterFactory, lambda _path: FakeArtifactWriter()
            ),
        ),
    )
    absent_request = _request(components, tmp_path / "absent", suffix="absent")
    absent_request = absent_request.model_copy(
        update={"selected_output": optional_null}
    )
    null_request = _request(components, tmp_path / "null", suffix="null").model_copy(
        update={"selected_output": optional_null}
    )
    array_request = _request(components, tmp_path / "array", suffix="array").model_copy(
        update={"selected_output": required_array}
    )

    absent = await runner.execute(absent_request)
    present_null = await runner.execute(null_request)
    present_array = await runner.execute(array_request)

    assert absent.selected_output == SelectedOutputAbsent()
    assert present_null.selected_output == SelectedOutputPresent(value=None)
    assert present_array.selected_output == SelectedOutputPresent(value=[1, 2])
    terminal_schemas = [
        next(tool for tool in request.tools if tool.name == "submit").input_schema
        for request in model.requests
    ]
    assert terminal_schemas[0]["properties"]["candidate"] == {"type": "null"}
    assert "candidate" not in terminal_schemas[0]["required"]
    assert terminal_schemas[2]["properties"]["candidate"] == required_array.json_schema
    assert "candidate" in terminal_schemas[2]["required"]


def test_selected_output_runtime_admission_rejects_all_bounded_failure_shapes() -> None:
    record = SelectedOutputRequirement(
        required=True,
        json_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    with pytest.raises(ValueError, match="missing"):
        admit_selected_output(record, present=False)
    with pytest.raises(ValueError, match="must be string"):
        admit_selected_output(record, present=True, value={"answer": 1})
    with pytest.raises(ValueError, match="is required"):
        admit_selected_output(record, present=True, value={})
    with pytest.raises(ValueError, match="is not allowed"):
        admit_selected_output(
            record,
            present=True,
            value={"answer": "ok", "prose": "not authority"},
        )

    string_requirement = SelectedOutputRequirement(
        required=True,
        json_schema={"type": "string"},
    )
    with pytest.raises(ValueError, match="string ceiling"):
        admit_selected_output(string_requirement, present=True, value="x" * 65_537)

    array_requirement = SelectedOutputRequirement(
        required=True,
        json_schema={"type": "array", "items": {"type": "null"}},
    )
    with pytest.raises(ValueError, match="array-item ceiling"):
        admit_selected_output(
            array_requirement,
            present=True,
            value=[None] * 1_025,
        )

    object_requirement = SelectedOutputRequirement(
        required=True,
        json_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    )
    with pytest.raises(ValueError, match="object-property ceiling"):
        admit_selected_output(
            object_requirement,
            present=True,
            value={f"field_{index}": None for index in range(65)},
        )

    nested: Any = None
    for _ in range(17):
        nested = [nested]
    with pytest.raises(ValueError, match="nesting-depth ceiling"):
        admit_selected_output(array_requirement, present=True, value=nested)

    payload = ["x" * 65_536 for _ in range(17)]
    payload_requirement = SelectedOutputRequirement(
        required=True,
        json_schema={"type": "array", "items": {"type": "string"}},
    )
    with pytest.raises(ValueError, match="payload-byte ceiling"):
        admit_selected_output(payload_requirement, present=True, value=payload)


def test_provider_tool_argument_parser_rejects_duplicate_and_nonfinite_candidates() -> (
    None
):
    duplicate = model_backend_module._normalize_tool_arguments(
        '{"terminal_result":"COMPLETE","summary":"done",'
        '"candidate":{"answer":1,"answer":2}}'
    )
    nonfinite = model_backend_module._normalize_tool_arguments(
        '{"terminal_result":"COMPLETE","summary":"done","candidate":NaN}'
    )
    overflow = model_backend_module._normalize_tool_arguments(
        '{"terminal_result":"COMPLETE","summary":"done","candidate":1e999}'
    )

    assert isinstance(duplicate, InvalidToolArguments)
    assert duplicate.error_code == "ambiguous_json"
    assert isinstance(nonfinite, InvalidToolArguments)
    assert nonfinite.error_code == "ambiguous_json"
    assert isinstance(overflow, InvalidToolArguments)
    assert overflow.error_code == "non_finite_json"


@pytest.mark.asyncio
async def test_selected_output_iteration_exhaustion_fails_closed_without_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components = _components(monkeypatch, tmp_path)
    profile_id = components.compiled_plan.model_profile.profile_id
    requirement = SelectedOutputRequirement(
        required=True,
        json_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    request = _request(components, tmp_path).model_copy(
        update={"selected_output": requirement}
    )
    model = FakeModelClient(
        responses=[
            _terminal_response(
                profile_id,
                candidate={"answer": index},
                call_suffix=f"invalid-{index}",
            )
            for index in range(components.compiled_plan.budgets.max_iterations)
        ]
    )
    writer = FakeArtifactWriter()
    runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=model,
            clock=FakeClock(monotonic_value=1.0),
            cancellation_resolver=FakeCancellationResolver(),
            artifact_writer_factory=cast(
                RuntimeArtifactWriterFactory, lambda _path: writer
            ),
        ),
    )

    result = await runner.execute(request)

    assert result.status is ExecutionStatus.INTERRUPTED
    assert result.result_class is ExecutionResultClass.BUDGET_EXHAUSTED
    assert result.terminal_intent is None
    assert result.selected_output is None
    assert result.selected_output_schema_sha256 is None
    assert writer.terminal_result_calls == []


@pytest.mark.asyncio
async def test_selected_output_null_content_malformed_arguments_exhaust_tool_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components = _components(monkeypatch, tmp_path)
    profile_id = components.compiled_plan.model_profile.profile_id
    requirement = SelectedOutputRequirement(
        required=True,
        json_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    request = _request(components, tmp_path).model_copy(
        update={"selected_output": requirement}
    )
    model = FakeModelClient(
        responses=[
            _invalid_terminal_response(profile_id, call_suffix=f"invalid-{index}")
            for index in range(components.compiled_plan.budgets.max_tool_errors + 1)
        ]
    )
    writer = FakeArtifactWriter()
    runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=model,
            clock=FakeClock(monotonic_value=1.0),
            cancellation_resolver=FakeCancellationResolver(),
            artifact_writer_factory=cast(
                RuntimeArtifactWriterFactory, lambda _path: writer
            ),
        ),
    )

    result = await runner.execute(request)

    assert result.status is ExecutionStatus.FAILED
    assert result.result_class is ExecutionResultClass.MODEL_FAILURE
    assert model.call_count == components.compiled_plan.budgets.max_tool_errors + 1
    assert result.terminal_intent is None
    assert result.selected_output is None
    assert result.selected_output_schema_sha256 is None
    assert writer.terminal_result_calls == []


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
    monkeypatch.setattr(
        runner_module,
        "_load_invocation_executor",
        lambda: pytest.fail("executor loaded before request binding verification"),
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
                update={"stage": request.stage.model_copy(update={"plane": "planning"})}
            ),
        ),
        (
            "stage_kind",
            request.model_copy(
                update={"stage": request.stage.model_copy(update={"node_id": "other"})}
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
async def test_opaque_correlation_values_round_trip_through_runtime_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    components = _components(monkeypatch, tmp_path)
    model_id = components.compiled_plan.model_profile.profile_id
    model = FakeModelClient(responses=[_tool_response(model_id, terminal="COMPLETE")])
    writer = FakeArtifactWriter()
    runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=model,
            clock=FakeClock(monotonic_value=1.0),
            cancellation_resolver=FakeCancellationResolver(),
            artifact_writer_factory=cast(
                RuntimeArtifactWriterFactory, lambda _path: writer
            ),
        ),
    )
    request_id = "opaque request correlation: alpha/7"
    run_id = "opaque run correlation: beta?attempt=42"
    request = _request(
        components,
        tmp_path,
        request_id=request_id,
        run_id=run_id,
    )

    restored = HarnessExecutionRequest.model_validate_json(request.model_dump_json())
    result = await runner.execute(restored)

    assert restored.request_id == request_id
    assert restored.run_id == run_id
    assert result.request_id == request_id
    assert result.run_id == run_id
    assert result.stage == request.stage
    assert result.terminal_intent is not None
    assert result.terminal_intent.request_id == request_id
    assert result.terminal_intent.run_id == run_id
    assert result.terminal_intent.stage == request.stage

    model_context = model.requests[0].messages[1].content
    assert model_context is not None
    assert request_id in model_context
    assert run_id in model_context

    events = writer.events_calls[0][1]
    trace = writer.tool_trace_calls[0][1]
    assert events
    assert trace
    for record in (*events, *trace):
        assert record["request_id"] == request_id
        assert record["run_id"] == run_id
        assert record["stage"] == request.stage.model_dump(mode="json")

    terminal_evidence = writer.terminal_result_calls[0][1]
    execution_evidence = writer.execution_summary_calls[0][1]
    metrics_evidence = writer.metrics_calls[0][1]
    manifest_evidence = writer.manifest_calls[0][1]
    for evidence in (
        terminal_evidence,
        execution_evidence,
        metrics_evidence,
        manifest_evidence,
    ):
        assert evidence["request_id"] == request_id
        assert evidence["run_id"] == run_id
    assert terminal_evidence["stage"] == request.stage.model_dump(mode="json")
    assert execution_evidence["stage"] == request.stage.model_dump(mode="json")

    invocation_evidence = runner.invocation_evidence_for(restored)
    assert invocation_evidence.request_id == request_id
    assert invocation_evidence.run_id == run_id
    assert (
        type(invocation_evidence).model_validate_json(
            invocation_evidence.model_dump_json()
        )
        == invocation_evidence
    )


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
    invocation_evidence: list[MillforgeInvocationEvidence] = []
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
    build_evidence = runner_module._build_invocation_evidence

    def record_invocation_evidence(
        evidence_components: MillforgeBaseComponents,
        descriptor: MillforgeBaseRunnerDescriptor,
        *,
        request_id: str,
        run_id: str,
        selected_output: SelectedOutputRequirement | None = None,
    ) -> MillforgeInvocationEvidence:
        evidence = build_evidence(
            evidence_components,
            descriptor,
            request_id=request_id,
            run_id=run_id,
            selected_output=selected_output,
        )
        invocation_evidence.append(evidence)
        return evidence

    monkeypatch.setattr(
        runner_module, "_build_invocation_evidence", record_invocation_evidence
    )
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
    assert [item.request_id for item in invocation_evidence] == [
        "request-first",
        "request-second",
        "request-first",
        "request-second",
    ]
    assert [item.run_id for item in invocation_evidence] == [
        "run-first",
        "run-second",
        "run-first",
        "run-second",
    ]
    assert len({id(item) for item in invocation_evidence}) == 4


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


class _LiveSecretResolver:
    def __init__(self, secret_ref: SecretRef, raw_value: str) -> None:
        self.secret_ref = secret_ref
        self._raw_value = raw_value
        self.resolve_calls: list[SecretRef] = []

    def resolve(self, ref: SecretRef) -> ResolvedSecret:
        self.resolve_calls.append(ref)
        if ref != self.secret_ref:
            raise AssertionError("unexpected secret reference")
        return ResolvedSecret(self._raw_value)


class _RecordingHttpTransport:
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []
        self.close_calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._handler(request)

    async def aclose(self) -> None:
        self.close_calls += 1


class _RecordingMockTransport(httpx.MockTransport):
    def __init__(self, handler: Any) -> None:
        super().__init__(handler)
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


class _ConstructionFailingSecretResolver:
    """Detect an accidental construction-time secret-resolution attempt."""

    def __init__(self) -> None:
        self.resolve_calls = 0

    def resolve(self, _ref: SecretRef) -> ResolvedSecret:
        self.resolve_calls += 1
        raise AssertionError("secret resolution must not run during construction")


class _TriggerCancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancellation_id(self) -> str:
        return "live-cancellation"

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    @property
    def reason(self) -> str | None:
        return "caller cancelled live request" if self.is_cancelled() else None

    def cancel(self) -> None:
        self._event.set()


class _TriggerCancellationResolver:
    def __init__(self, token: _TriggerCancellationToken) -> None:
        self.token = token

    def resolve(self, _ref: CancellationRef) -> _TriggerCancellationToken:
        return self.token


def _patch_live_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        composition,
        "resolve_pi_compat_shell",
        lambda: PiCompatShellConfig(executable="/test/bin/bash", arguments=("-c",)),
    )


def _live_timeouts() -> OpenAICompatibleTimeouts:
    return OpenAICompatibleTimeouts(
        connect_seconds=2,
        read_seconds=7,
        write_seconds=3,
        pool_seconds=1,
        local_total_seconds=5,
    )


@pytest.mark.asyncio
async def test_public_live_factory_completes_offline_traversal_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_live_shell(monkeypatch)
    (tmp_path / "note.txt").write_text("offline proof\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    profile = make_canonical_builder_profile_a()
    secret_ref = cast(SecretRef, profile.authentication.secret_ref)
    raw_secret = "sk-live-factory-secret"
    secret_resolver = _LiveSecretResolver(secret_ref, raw_secret)
    response_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal response_count
        response_count += 1
        body = json.loads(request.content)
        assert body["model"] == profile.model_id
        assert request.headers["authorization"] == f"Bearer {raw_secret}"
        assert request.extensions["timeout"] == {
            "connect": 2,
            "read": 5,
            "write": 3,
            "pool": 1,
        }
        if response_count == 1:
            message = {
                "role": "assistant",
                "content": "Read the note.",
                "tool_calls": [
                    {
                        "id": "call-read",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"path":"note.txt"}',
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            message = {
                "role": "assistant",
                "content": "Complete.",
                "tool_calls": [
                    {
                        "id": "call-submit",
                        "type": "function",
                        "function": {
                            "name": "submit",
                            "arguments": (
                                '{"summary":"offline complete",'
                                '"terminal_result":"COMPLETE"}'
                            ),
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "model": profile.model_id,
                "choices": [{"finish_reason": finish_reason, "message": message}],
            },
            request=request,
        )

    injected_transport = _RecordingHttpTransport(handler)
    assert isinstance(injected_transport, AsyncHttpTransport)
    live_runner = await create_millforge_base_live_runner(
        profile_id=profile.profile_id,
        model_profile=profile,
        secret_ref=secret_ref,
        secret_resolver=secret_resolver,
        cwd=tmp_path.resolve(),
        clock=FakeClock(monotonic_value=1.0),
        cancellation_resolver=FakeCancellationResolver(),
        artifact_writer_factory=cast(
            RuntimeArtifactWriterFactory, lambda _path: FakeArtifactWriter()
        ),
        timeouts=_live_timeouts(),
        http_transport=injected_transport,
        prompt_date=datetime.date(2026, 7, 15),
        home_directory=home.resolve(),
    )
    request = _request(live_runner.components, tmp_path / "run-live").model_copy(
        update={"secret_refs": (secret_ref,)}
    )
    assert injected_transport.requests == []
    assert secret_resolver.resolve_calls == []

    async with live_runner:
        result = await live_runner.execute(request)

    assert result.status is ExecutionStatus.COMPLETED
    assert result.terminal_intent is not None
    assert result.terminal_intent.terminal_result == "COMPLETE"
    assert response_count == 2
    assert live_runner.is_closed is True
    assert injected_transport.close_calls == 0
    assert secret_resolver.resolve_calls == [secret_ref, secret_ref]
    assert raw_secret not in repr(
        (profile, live_runner.components, live_runner.descriptor, result)
    )

    close_task = live_runner._close_task
    assert close_task is not None
    await asyncio.gather(live_runner.aclose(), live_runner.aclose())
    assert live_runner._close_task is close_task
    assert injected_transport.close_calls == 0
    with pytest.raises(MillforgeBaseClosedError, match="live runner is closed"):
        await live_runner.execute(request)


@pytest.mark.asyncio
async def test_public_live_factory_accepts_mock_transport_without_secret_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_live_shell(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    profile = make_canonical_builder_profile_a()
    secret_ref = cast(SecretRef, profile.authentication.secret_ref)
    secret_resolver = _ConstructionFailingSecretResolver()
    mock_transport = _RecordingMockTransport(
        lambda request: httpx.Response(500, request=request)
    )

    assert isinstance(mock_transport, AsyncHttpTransport)
    live_runner = await create_millforge_base_live_runner(
        profile_id=profile.profile_id,
        model_profile=profile,
        secret_ref=secret_ref,
        secret_resolver=secret_resolver,
        cwd=tmp_path.resolve(),
        clock=FakeClock(),
        cancellation_resolver=FakeCancellationResolver(),
        timeouts=_live_timeouts(),
        http_transport=mock_transport,
        prompt_date=datetime.date(2026, 7, 15),
        home_directory=home.resolve(),
    )

    assert secret_resolver.resolve_calls == 0
    await live_runner.aclose()
    assert mock_transport.close_calls == 0

    with pytest.raises(
        ModelBackendConfigError, match="does not implement AsyncHttpTransport"
    ):
        await create_millforge_base_live_runner(
            profile_id=profile.profile_id,
            model_profile=profile,
            secret_ref=secret_ref,
            secret_resolver=secret_resolver,
            cwd=tmp_path.resolve(),
            clock=FakeClock(),
            cancellation_resolver=FakeCancellationResolver(),
            timeouts=_live_timeouts(),
            http_transport=cast(AsyncHttpTransport, object()),
            prompt_date=datetime.date(2026, 7, 15),
            home_directory=home.resolve(),
        )
    assert secret_resolver.resolve_calls == 0

    with pytest.raises(
        SecretResolutionError, match="does not implement SecretResolver"
    ):
        await create_millforge_base_live_runner(
            profile_id=profile.profile_id,
            model_profile=profile,
            secret_ref=secret_ref,
            secret_resolver=cast(Any, object()),
            cwd=tmp_path.resolve(),
            clock=FakeClock(),
            cancellation_resolver=FakeCancellationResolver(),
            timeouts=_live_timeouts(),
            prompt_date=datetime.date(2026, 7, 15),
            home_directory=home.resolve(),
        )


@pytest.mark.asyncio
async def test_live_factory_rejects_identity_and_cleans_partial_owned_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_live_shell(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    profile = make_canonical_builder_profile_a()
    secret_ref = cast(SecretRef, profile.authentication.secret_ref)
    raw_secret = "sk-partial-failure-secret"
    secret_resolver = _LiveSecretResolver(secret_ref, raw_secret)

    with pytest.raises(
        ModelBackendConfigError, match="profile IDs must match"
    ) as caught:
        await create_millforge_base_live_runner(
            profile_id="unknown-profile",
            model_profile=profile,
            secret_ref=secret_ref,
            secret_resolver=secret_resolver,
            cwd=tmp_path.resolve(),
            clock=FakeClock(),
            cancellation_resolver=FakeCancellationResolver(),
            timeouts=_live_timeouts(),
            prompt_date=datetime.date(2026, 7, 15),
            home_directory=home.resolve(),
        )
    assert raw_secret not in repr(caught.value)

    caller_transport = _RecordingHttpTransport(
        lambda request: httpx.Response(500, request=request)
    )
    created_transports: list[Any] = []

    class RecordingOwnedTransport(model_backend_module.OpenAIChatCompletionsTransport):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.close_calls = 0
            created_transports.append(self)

        async def aclose(self) -> None:
            self.close_calls += 1
            await super().aclose()

    monkeypatch.setattr(
        runner_module, "OpenAIChatCompletionsTransport", RecordingOwnedTransport
    )

    def fail_runner_construction(**_kwargs: Any) -> Any:
        raise RuntimeError("injected construction failure")

    monkeypatch.setattr(
        runner_module, "create_millforge_base_runner", fail_runner_construction
    )
    with pytest.raises(ModelBackendConfigError, match="construction failed"):
        await create_millforge_base_live_runner(
            profile_id=profile.profile_id,
            model_profile=profile,
            secret_ref=secret_ref,
            secret_resolver=secret_resolver,
            cwd=tmp_path.resolve(),
            clock=FakeClock(),
            cancellation_resolver=FakeCancellationResolver(),
            timeouts=_live_timeouts(),
            http_transport=caller_transport,
            prompt_date=datetime.date(2026, 7, 15),
            home_directory=home.resolve(),
        )

    assert len(created_transports) == 1
    assert created_transports[0].close_calls == 1
    assert created_transports[0]._client.is_closed is True
    assert caller_transport.close_calls == 0


@pytest.mark.asyncio
async def test_public_live_factory_cancellation_interrupts_blocked_model_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_live_shell(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    profile = make_canonical_builder_profile_a()
    secret_ref = cast(SecretRef, profile.authentication.secret_ref)
    secret_resolver = _LiveSecretResolver(secret_ref, "sk-cancelled-secret")
    token = _TriggerCancellationToken()
    cancellation_resolver = _TriggerCancellationResolver(token)
    started = asyncio.Event()
    interrupted = asyncio.Event()

    async def blocked_handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            interrupted.set()
            raise
        raise AssertionError("blocked handler unexpectedly resumed")

    live_runner = await create_millforge_base_live_runner(
        profile_id=profile.profile_id,
        model_profile=profile,
        secret_ref=secret_ref,
        secret_resolver=secret_resolver,
        cwd=tmp_path.resolve(),
        clock=FakeClock(monotonic_value=1.0),
        cancellation_resolver=cancellation_resolver,
        artifact_writer_factory=cast(
            RuntimeArtifactWriterFactory, lambda _path: FakeArtifactWriter()
        ),
        timeouts=_live_timeouts(),
        http_transport=_RecordingHttpTransport(blocked_handler),
        prompt_date=datetime.date(2026, 7, 15),
        home_directory=home.resolve(),
    )
    request = _request(live_runner.components, tmp_path / "run-cancel").model_copy(
        update={"secret_refs": (secret_ref,)}
    )
    execution = asyncio.create_task(live_runner.execute(request))
    await asyncio.wait_for(started.wait(), timeout=1)

    token.cancel()
    result = await asyncio.wait_for(execution, timeout=1)

    assert interrupted.is_set()
    assert result.status is ExecutionStatus.INTERRUPTED
    assert result.result_class is ExecutionResultClass.CANCELLED
    await live_runner.aclose()


def test_runner_uses_components_terminal_vocabulary_for_descriptor_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vocabulary = ("BLOCKED", "COMPLETE", "FOO_BLOCKED", "REJECTED")
    components = _components(
        monkeypatch,
        tmp_path,
        legal_terminal_results=vocabulary,
    )

    runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=FakeModelClient(),
            clock=FakeClock(),
            cancellation_resolver=FakeCancellationResolver(),
        ),
    )

    assert runner.descriptor.legal_terminal_result_ids == vocabulary
    assert (
        runner.descriptor.tool_catalog_sha256
        == components.tool_snapshot.snapshot_sha256
    )


def test_configured_descriptor_drift_refuses_before_model_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vocabulary = ("BLOCKED", "COMPLETE", "ESCALATED", "REJECTED")
    components = _components(
        monkeypatch,
        tmp_path,
        legal_terminal_results=vocabulary,
    )
    model = FakeModelClient()
    tampered = replace(
        components,
        legal_terminal_results=("BLOCKED", "COMPLETE", "REJECTED", "REVIEW"),
    )

    with pytest.raises(MillforgeBaseBindingError) as caught:
        create_millforge_base_runner(
            components=tampered,
            services=MillforgeBaseRuntimeServices(
                model_client=model,
                clock=FakeClock(),
                cancellation_resolver=FakeCancellationResolver(),
            ),
        )

    assert caught.value.reason == "backend_composition"
    assert model.call_count == 0


@pytest.mark.asyncio
async def test_configured_four_result_lifecycle_preserves_exact_invocation_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vocabulary = ("BLOCKED", "COMPLETE", "FOO_BLOCKED", "REJECTED")
    components = _components(
        monkeypatch,
        tmp_path,
        legal_terminal_results=vocabulary,
    )

    for terminal_result in vocabulary:
        model = FakeModelClient(
            responses=[
                _configured_terminal_response(
                    components.model_profile.profile_id,
                    terminal_result=terminal_result,
                )
            ]
        )
        runner = create_millforge_base_runner(
            components=components,
            services=MillforgeBaseRuntimeServices(
                model_client=model,
                clock=FakeClock(monotonic_value=1.0),
                cancellation_resolver=FakeCancellationResolver(),
                artifact_writer_factory=cast(
                    RuntimeArtifactWriterFactory, lambda _path: FakeArtifactWriter()
                ),
            ),
        )
        request = _request(
            components,
            tmp_path / terminal_result.lower(),
            suffix=terminal_result.lower(),
        )

        evidence = runner.invocation_evidence_for(request)
        result = await runner.execute(request)

        assert evidence.descriptor_sha256 == runner.descriptor.descriptor_sha256
        assert result.terminal_intent is not None
        assert result.terminal_intent.terminal_result == terminal_result
        assert (
            result.terminal_intent.disposition
            == {
                "BLOCKED": "blocked",
                "COMPLETE": "success",
                "FOO_BLOCKED": "success",
                "REJECTED": "rejected",
            }[terminal_result]
        )


@pytest.mark.asyncio
async def test_configured_terminal_preserves_selected_output_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vocabulary = ("BLOCKED", "COMPLETE", "ESCALATED", "REJECTED")
    components = _components(
        monkeypatch,
        tmp_path,
        legal_terminal_results=vocabulary,
    )
    selected_output_schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    required_output = SelectedOutputRequirement(
        required=True,
        json_schema=selected_output_schema,
    )
    optional_output = SelectedOutputRequirement(
        required=False,
        json_schema=selected_output_schema,
    )
    model = FakeModelClient(
        responses=[
            _configured_terminal_response(
                components.model_profile.profile_id,
                terminal_result="ESCALATED",
                candidate={"answer": 7},
            ),
            _configured_terminal_response(
                components.model_profile.profile_id,
                terminal_result="ESCALATED",
            ),
            _configured_terminal_response(
                components.model_profile.profile_id,
                terminal_result="ESCALATED",
            ),
        ]
    )
    runner = create_millforge_base_runner(
        components=components,
        services=MillforgeBaseRuntimeServices(
            model_client=model,
            clock=FakeClock(monotonic_value=1.0),
            cancellation_resolver=FakeCancellationResolver(),
            artifact_writer_factory=cast(
                RuntimeArtifactWriterFactory, lambda _path: FakeArtifactWriter()
            ),
        ),
    )
    request = _request(components, tmp_path / "selected", suffix="selected")
    required_request = request.model_copy(update={"selected_output": required_output})
    optional_request = request.model_copy(update={"selected_output": optional_output})

    required_evidence = runner.invocation_evidence_for(required_request)
    optional_evidence = runner.invocation_evidence_for(optional_request)
    no_output_evidence = runner.invocation_evidence_for(request)
    required_result = await runner.execute(required_request)
    optional_result = await runner.execute(optional_request)
    no_output_result = await runner.execute(request)

    assert required_result.terminal_intent is not None
    assert required_result.terminal_intent.terminal_result == "ESCALATED"
    assert required_result.selected_output == SelectedOutputPresent(value={"answer": 7})
    assert optional_result.selected_output == SelectedOutputAbsent()
    assert no_output_result.selected_output is None
    assert no_output_result.selected_output_schema_sha256 is None
    assert (
        required_evidence.selected_output_schema_sha256,
        required_evidence.selected_output_required,
    ) == (required_output.schema_sha256, True)
    assert (
        optional_evidence.selected_output_schema_sha256,
        optional_evidence.selected_output_required,
    ) == (optional_output.schema_sha256, False)
    assert (
        no_output_evidence.selected_output_schema_sha256,
        no_output_evidence.selected_output_required,
    ) == (None, None)
    assert required_output.schema_sha256 == optional_output.schema_sha256
    assert required_evidence.invocation_sha256 != optional_evidence.invocation_sha256
    assert optional_evidence.invocation_sha256 != no_output_evidence.invocation_sha256
    required_tool, optional_tool, no_output_tool = (
        next(tool for tool in model_request.tools if tool.name == "terminal_escalated")
        for model_request in model.requests
    )
    assert (
        required_tool.input_schema["properties"]["candidate"]
        == required_output.json_schema
    )
    assert "candidate" in required_tool.input_schema["required"]
    assert (
        optional_tool.input_schema["properties"]["candidate"]
        == optional_output.json_schema
    )
    assert "candidate" not in optional_tool.input_schema["required"]
    assert "candidate" not in no_output_tool.input_schema["properties"]


@pytest.mark.asyncio
async def test_live_factory_propagates_configured_terminal_vocabulary_and_closes_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_live_shell(monkeypatch)
    vocabulary = ("BLOCKED", "COMPLETE", "ESCALATED", "REJECTED")
    home = tmp_path / "home"
    home.mkdir()
    profile = make_canonical_builder_profile_a()
    secret_ref = cast(SecretRef, profile.authentication.secret_ref)
    secret_resolver = _LiveSecretResolver(secret_ref, "sk-configured-live")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == profile.model_id
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "model": profile.model_id,
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "Escalate.",
                            "tool_calls": [
                                {
                                    "id": "call-terminal-escalated",
                                    "type": "function",
                                    "function": {
                                        "name": "terminal_escalated",
                                        "arguments": (
                                            '{"summary":"configured live",'
                                            '"terminal_result":"ESCALATED"}'
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
            request=request,
        )

    live_runner = await create_millforge_base_live_runner(
        legal_terminal_results=vocabulary,
        profile_id=profile.profile_id,
        model_profile=profile,
        secret_ref=secret_ref,
        secret_resolver=secret_resolver,
        cwd=tmp_path.resolve(),
        clock=FakeClock(monotonic_value=1.0),
        cancellation_resolver=FakeCancellationResolver(),
        artifact_writer_factory=cast(
            RuntimeArtifactWriterFactory, lambda _path: FakeArtifactWriter()
        ),
        timeouts=_live_timeouts(),
        http_transport=_RecordingHttpTransport(handler),
        prompt_date=datetime.date(2026, 7, 15),
        home_directory=home.resolve(),
    )
    request = _request(
        live_runner.components, tmp_path / "run-configured-live"
    ).model_copy(update={"secret_refs": (secret_ref,)})

    result = await live_runner.execute(request)
    await live_runner.aclose()
    close_task = live_runner._close_task
    await live_runner.aclose()

    assert live_runner.components.legal_terminal_results == vocabulary
    assert live_runner.descriptor.legal_terminal_result_ids == vocabulary
    assert result.terminal_intent is not None
    assert result.terminal_intent.terminal_result == "ESCALATED"
    assert close_task is not None
    assert live_runner._close_task is close_task
