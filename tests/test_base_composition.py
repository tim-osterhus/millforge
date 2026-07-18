"""Bounded composition and offline runtime proof for Millforge base."""

from __future__ import annotations

import datetime
import hashlib

import pytest

from millforge._forge.adapter import ForgeContextFactory, ForgeGuardrailBackend
from millforge.base import composition, context, prompt
from millforge.base.composition import create_millforge_base_components
from millforge.contracts import (
    AssistantMessage,
    CancellationRef,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    CompiledHarnessRef,
    HarnessExecutionRequest,
    HarnessTaskInput,
    ModelCompletionResponse,
    ModelCompletionRequest,
    ModelProfileRef,
    ModelToolCall,
    ParsedToolArguments,
    RunDirRef,
    StageIdentity,
    TimeoutRef,
)
from millforge.model_backend import (
    CapabilityDeclarations,
    CapabilitySupport,
    UnsupportedModelCapabilityError,
)
from millforge.runtime import DefaultHarnessRuntime, ExecutionStatus
from millforge.testing import FakeModelClient
from millforge.tools.pi_compat.process import PiCompatShellConfig
from tests.conftest import (
    FakeArtifactWriter,
    FakeCancellationResolver,
    FakeClock,
    FakePlanLoader,
    make_canonical_builder_profile_a,
)


def _fake_shell_config() -> PiCompatShellConfig:
    return PiCompatShellConfig(executable="/test/bin/bash", arguments=("-c",))


def _components(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    shell_config = _fake_shell_config()
    shell_resolutions: list[PiCompatShellConfig] = []
    executor_shell_configs: list[PiCompatShellConfig | None] = []
    original_executor_factory = (
        composition._create_pi_compat_tool_executor_for_terminal_results
    )

    def resolve_shell() -> PiCompatShellConfig:
        shell_resolutions.append(shell_config)
        return shell_config

    def create_executor(*args, **kwargs):
        executor_shell_configs.append(kwargs["shell_config"])
        return original_executor_factory(*args, **kwargs)

    monkeypatch.setattr(composition, "resolve_pi_compat_shell", resolve_shell)
    monkeypatch.setattr(
        composition,
        "_create_pi_compat_tool_executor_for_terminal_results",
        create_executor,
    )
    home_directory = tmp_path / "home"
    home_directory.mkdir()
    components = create_millforge_base_components(
        model_profile=make_canonical_builder_profile_a(),
        cwd=tmp_path.resolve(),
        cancellation_resolver=FakeCancellationResolver(),
        prompt_date=datetime.date(2026, 7, 15),
        home_directory=home_directory.resolve(),
    )
    return components, shell_config, shell_resolutions, executor_shell_configs


def test_composition_admits_model_reuses_shell_and_emits_sanitized_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    profile = make_canonical_builder_profile_a()
    unsupported = profile.model_copy(
        update={
            "capabilities": CapabilityDeclarations(
                support={"tool_calls": CapabilitySupport.UNSUPPORTED}
            )
        }
    )
    source_calls: list[object] = []

    def source_must_not_be_built(**_kwargs):
        source_calls.append(object())
        raise AssertionError("source construction must follow model admission")

    monkeypatch.setattr(
        composition,
        "_millforge_base_harness_source_for_terminal_results",
        source_must_not_be_built,
    )
    with pytest.raises(
        UnsupportedModelCapabilityError, match="supported model capabilities"
    ):
        create_millforge_base_components(
            model_profile=unsupported,
            cwd=tmp_path.resolve(),
            cancellation_resolver=FakeCancellationResolver(),
            prompt_date=datetime.date(2026, 7, 15),
            home_directory=tmp_path.resolve(),
        )
    assert source_calls == []

    monkeypatch.undo()
    components, shell_config, shell_resolutions, executor_shell_configs = _components(
        monkeypatch, tmp_path
    )

    assert shell_resolutions == [shell_config]
    assert executor_shell_configs == [shell_config]
    assert tuple(
        grant.capability_id for grant in components.capability_envelope.grants
    ) == (components.compiled_plan.required_capabilities)
    assert all(
        grant.constraints is None for grant in components.capability_envelope.grants
    )
    assert components.metadata.model_dump() == {
        "schema_version": 1,
        "config_id": "millforge-base.v1",
        "harness_id": "millforge.base.unrestricted_agent.v1",
        "tool_pack_id": "millforge.toolpack.pi_compat.unrestricted.v1",
        "upstream_package": "@earendil-works/pi-coding-agent",
        "upstream_version": "0.79.6",
        "compatibility_claim": (
            "A Python behavioral port of Pi 0.79.6's complete built-in coding tool pack, "
            "adapted to Millforge's compiler and runtime contracts."
        ),
        "security_warning": (
            "millforge-base runs with the permissions of the Millforge process. It can read, "
            "write, delete, execute commands, access the network, and access credentials "
            "available to that process. Use only in trusted environments."
        ),
        "unrestricted": True,
        "enabled_aliases": (
            "read",
            "bash",
            "edit",
            "write",
            "grep",
            "find",
            "ls",
            "submit",
            "block",
            "reject",
        ),
        "model_profile_id": profile.profile_id,
        "provider_id": profile.provider_id,
        "model_id": profile.model_id,
        "transport_id": profile.transport_id,
        "os_name": components.metadata.os_name,
        "shell_name": "bash",
        "cwd_sha256": hashlib.sha256(
            tmp_path.resolve().as_posix().encode("utf-8")
        ).hexdigest(),
        "descriptor_snapshot_sha256": components.tool_snapshot.snapshot_sha256,
        "compiled_sha256": components.compiled_plan.compiled_sha256,
        "effective_prompt_sha256": components.prompt.effective_prompt_sha256,
        "context_sha256": components.context.context_sha256,
        "context_file_count": 0,
        "context_truncated": False,
        "prompt_truncated": False,
    }
    metadata_json = components.metadata.model_dump_json()
    assert str(tmp_path.resolve()) not in metadata_json
    assert components.prompt.system_instructions not in metadata_json
    assert profile.endpoint.base_url not in metadata_json


def test_composition_reuses_captured_path_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    input_path_resolutions: list[object] = []

    def track_input_path_resolution(*args, **_kwargs):
        input_path_resolutions.append(args)
        return args[0]

    monkeypatch.setattr(context, "_resolved_absolute_path", track_input_path_resolution)
    monkeypatch.setattr(prompt, "_resolved_absolute_path", track_input_path_resolution)

    _components(monkeypatch, tmp_path)

    assert input_path_resolutions == []


@pytest.mark.asyncio
async def test_composition_plan_and_executor_complete_offline_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    (tmp_path / "note.txt").write_text("offline proof\n", encoding="utf-8")
    components, _shell_config, _resolutions, _executor_configs = _components(
        monkeypatch, tmp_path
    )
    plan = components.compiled_plan
    request = HarnessExecutionRequest(
        request_id="request-base-runtime",
        run_id="run-base-runtime",
        work_item_id="work-base-runtime",
        task=HarnessTaskInput(instruction="Read note.txt and complete the task."),
        stage=StageIdentity(
            plane="execution",
            node_id="millforge-base",
            stage_kind_id="millforge_base",
        ),
        compiled_harness=CompiledHarnessRef(
            identity=CompiledHarnessIdentity(
                compiled_plan_id="plan-base-runtime",
                harness_id=plan.harness_id,
                harness_version=plan.harness_version,
            ),
            path=tmp_path / "compiled-plan.json",
            expected_hash=CompiledHarnessHash(
                algorithm="sha256", digest=plan.compiled_sha256
            ),
        ),
        capability_envelope=components.capability_envelope,
        input_artifacts=(),
        run_directory=RunDirRef(run_id="run-base-runtime", path=tmp_path),
        timeout=TimeoutRef(timeout_seconds=60),
        cancellation=CancellationRef(cancellation_id="cancel-base-runtime"),
        secret_refs=(),
        model_profile=ModelProfileRef(profile_id=plan.model_profile.profile_id),
    )

    class InstructionGatedModelClient(FakeModelClient):
        async def complete(
            self, model_request: ModelCompletionRequest
        ) -> ModelCompletionResponse:
            user_messages = [
                message.content
                for message in model_request.messages
                if message.role == "user"
            ]
            if not user_messages or not user_messages[0].startswith(
                request.task.instruction
            ):
                raise AssertionError("task instruction was not model-visible")
            return await super().complete(model_request)

    model_client = InstructionGatedModelClient(
        responses=[
            ModelCompletionResponse(
                provider_request_id="provider-read",
                model_id=plan.model_profile.profile_id,
                message=AssistantMessage(
                    content="Read the local note.",
                    tool_calls=(
                        ModelToolCall(
                            call_id="call-read",
                            name="read",
                            arguments=ParsedToolArguments(value={"path": "note.txt"}),
                        ),
                    ),
                ),
                finish_reason="tool_calls",
                usage=None,
            ),
            ModelCompletionResponse(
                provider_request_id="provider-submit",
                model_id=plan.model_profile.profile_id,
                message=AssistantMessage(
                    content="Complete the run.",
                    tool_calls=(
                        ModelToolCall(
                            call_id="call-submit",
                            name="submit",
                            arguments=ParsedToolArguments(
                                value={"terminal_result": "COMPLETE", "summary": "done"}
                            ),
                        ),
                    ),
                ),
                finish_reason="tool_calls",
                usage=None,
            ),
        ]
    )
    clock = FakeClock(monotonic_value=1.0)
    cancellation_resolver = FakeCancellationResolver()
    plan_loader = FakePlanLoader(plan=plan)
    backend = ForgeGuardrailBackend(
        model_client=model_client,
        tool_executor=components.tool_executor,
        plan_loader=plan_loader,
        context_factory=ForgeContextFactory(),
        clock=clock,
        cancellation_resolver=cancellation_resolver,
    )
    artifact_writer = FakeArtifactWriter()
    runtime = DefaultHarnessRuntime(
        backend=backend,
        plan_loader=plan_loader,
        artifact_writer=artifact_writer,
        clock=clock,
        cancellation_resolver=cancellation_resolver,
    )

    result = await runtime.execute(request)

    assert result.status is ExecutionStatus.COMPLETED
    assert model_client.call_count == 2
    assert len(artifact_writer.tool_trace_calls) == 1
    assert [record["node_id"] for record in artifact_writer.tool_trace_calls[0][1]] == [
        "read",
        "submit",
    ]
    assert len(artifact_writer.terminal_result_calls) == 1
    persisted = repr(
        (
            artifact_writer.execution_summary_calls,
            artifact_writer.events_calls,
            artifact_writer.tool_trace_calls,
            artifact_writer.metrics_calls,
            artifact_writer.diagnostic_calls,
            result,
            components.metadata,
        )
    )
    assert request.task.instruction not in persisted
