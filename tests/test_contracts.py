"""Focused tests for the runtime-consumed 02B contract models."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast, get_args

import millforge
import pytest
from pydantic import ValidationError

from millforge.compiled_plan import (
    ArgumentMatch,
    CompilerIdentity,
    CompiledArtifactPolicy,
    CompiledBudgetPolicy,
    CompiledContextPolicy,
    CompiledHarnessNode,
    CompiledHarnessPlan,
    CompiledModelProfile,
    CompiledPrerequisite,
    CompiledPromptPolicy,
    IdempotencyClass,
    SessionEvent,
    SessionEventType,
    SideEffectCertainty,
    SideEffectClass,
    TerminalArtifactRequirement,
    ToolBindingRef,
    ToolTraceDecision,
    ToolTraceDecisionRecord,
    ToolTraceIdempotency,
    ToolTraceSideEffectClass,
    ToolExecutionStatus,
    ToolTraceRecord,
    calculate_compiled_plan_sha256,
    canonical_compiled_plan_bytes,
    canonical_json_serialize,
    finalize_compiled_plan_sha256,
    parse_and_strip_compiled_plan,
    verify_compiled_plan_sha256,
)
from millforge.contracts import (
    ArtifactRef,
    CancellationRef,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    CompiledHarnessRef,
    Deadline,
    DiagnosticField,
    DiagnosticMetadata,
    ExecutionResultClass,
    ExecutionStatus,
    HarnessExecutionRequest,
    HarnessExecutionResult,
    HarnessTaskInput,
    AssistantMessage,
    InvalidToolArguments,
    ModelCapabilityRequirements,
    ModelCompletionRequest,
    ModelCompletionResponse,
    ModelToolDefinition,
    ModelToolCall,
    ParsedToolArguments,
    RedactionPolicy,
    SamplingRequest,
    SanitizedMetadata,
    SideEffectRecord,
    StageIdentity,
    TerminalIntent,
    TimingMetadata,
    TokenUsage,
    TimeoutOrigin,
    ToolExecutionResult,
    ToolResultMessage,
    UsageMetadata,
    ValidatedToolCall,
    UserMessage,
    SystemMessage,
    redact_diagnostic_mapping,
    redact_diagnostic_text,
    redact_diagnostic_value,
)
from tests.conftest import (
    BUILDER_FIXTURE_COMPILED_SHA256,
    BUILDER_FIXTURE_DESCRIPTOR_SHA256,
    BUILDER_FIXTURE_HARNESS_ID,
    BUILDER_FIXTURE_HARNESS_VERSION,
    BUILDER_FIXTURE_PROFILE_ID,
    BUILDER_FIXTURE_PROMPT_SHA256,
    builder_fixture_prompt_sha256,
    make_canonical_builder_compiled_plan,
    make_canonical_builder_execution_request,
)


def test_harness_task_input_preserves_utf8_bytes_and_exposes_stable_metadata() -> None:
    instruction = "  first line\r\nsecond line: caf\N{LATIN SMALL LETTER E WITH ACUTE} \N{ROCKET}  "
    encoded = instruction.encode("utf-8")

    task = HarnessTaskInput(instruction=instruction)

    assert task.instruction.encode("utf-8") == encoded
    assert task.utf8_byte_count == len(encoded)
    assert task.sha256 == hashlib.sha256(encoded).hexdigest()
    assert task.model_dump() == {
        "schema_version": "1.0",
        "instruction": instruction,
    }


@pytest.mark.parametrize(
    "instruction", ["", " \t\r\n", "before\x00after", "unpaired \ud800"]
)
def test_harness_task_input_rejects_invalid_instruction(instruction: str) -> None:
    with pytest.raises(ValidationError):
        HarnessTaskInput(instruction=instruction)


def test_harness_task_input_enforces_exact_utf8_byte_limit() -> None:
    exact_multibyte = "a" * 65_532 + "\N{ROCKET}"

    task = HarnessTaskInput(instruction=exact_multibyte)

    assert task.utf8_byte_count == 65_536
    with pytest.raises(ValidationError, match="65,536 UTF-8 bytes"):
        HarnessTaskInput(instruction=exact_multibyte + "a")


@pytest.mark.parametrize(
    "instruction",
    [
        "packet03-secret-sentinel\x00tail",
        "packet03-secret-sentinel" + "x" * 65_536,
    ],
)
def test_harness_task_input_validation_errors_hide_raw_instruction(
    instruction: str,
) -> None:
    with pytest.raises(ValidationError) as caught:
        HarnessTaskInput(instruction=instruction)

    assert "packet03-secret-sentinel" not in str(caught.value)


def test_nested_harness_request_validation_error_hides_raw_instruction(
    tmp_path: Path,
) -> None:
    request = make_canonical_builder_execution_request(tmp_path)
    payload = request.model_dump(mode="python")
    payload["task"] = {
        "schema_version": "1.0",
        "instruction": "packet03-nested-secret-sentinel\x00tail",
    }

    with pytest.raises(ValidationError) as caught:
        HarnessExecutionRequest.model_validate(payload)

    assert "packet03-nested-secret-sentinel" not in str(caught.value)


def test_harness_execution_request_requires_task_and_accepts_no_artifacts(
    tmp_path: Path,
) -> None:
    request = make_canonical_builder_execution_request(tmp_path)
    payload = request.model_dump(mode="python")
    payload["input_artifacts"] = ()

    restored = HarnessExecutionRequest.model_validate(payload)

    assert restored.input_artifacts == ()
    payload.pop("task")
    with pytest.raises(ValidationError):
        HarnessExecutionRequest.model_validate(payload)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _deadline() -> Deadline:
    return Deadline(
        started_monotonic=0.0,
        outer_deadline_monotonic=60.0,
        effective_deadline_monotonic=60.0,
        source="request",
    )


def _cancellation() -> CancellationRef:
    return CancellationRef(cancellation_id="cancel-1")


def _binding(tool_id: str = "tool.weather") -> ToolBindingRef:
    return ToolBindingRef(
        tool_id=tool_id,
        tool_version=1,
        descriptor_sha256=SHA_A,
        implementation_id="impl.weather.v1",
    )


def _node(
    node_id: str = "terminal",
    *,
    terminal_result: str | None = "success",
    required: bool = False,
    model_tool_name: str = "complete_success",
    produced_artifact_ids: tuple[str, ...] = ("summary",),
    prerequisites: tuple[CompiledPrerequisite, ...] = (),
) -> CompiledHarnessNode:
    return CompiledHarnessNode(
        node_id=node_id,
        model_tool_name=model_tool_name,
        description="Complete the stage",
        input_schema={"type": "object", "properties": {}},
        binding=_binding(),
        prerequisites=prerequisites,
        required=required,
        terminal_result=terminal_result,
        required_capabilities=("workspace.read",),
        produced_artifact_ids=produced_artifact_ids,
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    )


def _closed_schema(
    properties: dict[str, object],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(required),
    }


def _plan(compiled_sha256: str = SHA_B) -> CompiledHarnessPlan:
    return CompiledHarnessPlan(
        schema_version="1.0",
        kind="compiled_millforge_harness",
        harness_id="harness-1",
        harness_version=1,
        source_sha256=SHA_A,
        compiled_sha256=compiled_sha256,
        stage_kind_ids=("builder",),
        model_profile=CompiledModelProfile(profile_id="standard"),
        prompt_policy=CompiledPromptPolicy(
            policy_id="policy-1",
            system_instructions="Follow the stage contract.",
            include_request_context=True,
        ),
        budgets=CompiledBudgetPolicy(
            max_iterations=1,
            max_validation_retries=1,
            max_tool_errors=1,
            max_prerequisite_violations=1,
            max_premature_terminal_attempts=1,
        ),
        context_policy=CompiledContextPolicy(
            strategy_id="forge.tiered.v1",
            budget_tokens=4096,
            keep_recent_iterations=1,
            phase_thresholds=(0.25, 0.5, 1.0),
        ),
        nodes=(_node(),),
        required_capabilities=("workspace.read",),
        terminal_result_map={"terminal": "success"},
        artifact_policy=CompiledArtifactPolicy(
            declared_artifact_ids=("summary",),
            required_by_terminal=(
                TerminalArtifactRequirement(
                    terminal_result="success", artifact_ids=("summary",)
                ),
            ),
        ),
        compiler_identity=CompilerIdentity(
            name="millforge-test", version="1.0.0", build_id="test-build"
        ),
    )


def _plan_with_valid_hash() -> tuple[CompiledHarnessPlan, str]:
    plan = finalize_compiled_plan_sha256(_plan(compiled_sha256=SHA_B))
    return plan, plan.compiled_sha256


def _compiled_ref() -> CompiledHarnessRef:
    return CompiledHarnessRef(
        identity=CompiledHarnessIdentity(
            compiled_plan_id="compiled-1",
            harness_id="harness-1",
            harness_version=1,
        ),
        path=Path("/tmp/compiled-1.json"),
        expected_hash=CompiledHarnessHash(algorithm="sha256", digest=SHA_A),
    )


def _stage() -> StageIdentity:
    return StageIdentity(plane="execution", node_id="builder", stage_kind_id="builder")


def test_compiled_harness_plan_round_trip_and_hash_verification() -> None:
    plan, digest = _plan_with_valid_hash()
    raw = plan.model_dump_json()

    restored, stripped = parse_and_strip_compiled_plan(raw)
    assert restored == plan
    assert "compiled_sha256" not in stripped

    verified, computed, warnings, result = verify_compiled_plan_sha256(
        raw,
        expected_compiled_hash=digest,
        expected_harness_id="harness-1",
        expected_harness_version=1,
    )
    assert verified is True
    assert computed == digest
    assert warnings == []
    assert result == plan


def test_compiled_plan_hash_helper_is_the_single_contract_algorithm() -> None:
    placeholder = _plan(compiled_sha256=SHA_B)
    payload = placeholder.model_dump(mode="json")
    digest = calculate_compiled_plan_sha256(payload)
    finalized = finalize_compiled_plan_sha256(placeholder)

    manual_body = dict(payload)
    manual_body.pop("compiled_sha256")
    manual_digest = hashlib.sha256(
        canonical_json_serialize(manual_body).encode("utf-8")
    ).hexdigest()

    assert digest == manual_digest
    assert finalized.compiled_sha256 == digest
    assert finalized.model_dump(mode="json")["compiled_sha256"] == digest
    assert canonical_compiled_plan_bytes(finalized) == canonical_json_serialize(
        finalized.model_dump(mode="json")
    ).encode("utf-8")


def test_compiled_plan_hash_helper_requires_complete_payload() -> None:
    payload = _plan().model_dump(mode="json")
    payload.pop("compiled_sha256")

    with pytest.raises(ValueError, match="must include compiled_sha256"):
        calculate_compiled_plan_sha256(payload)


def test_canonical_compiled_plan_bytes_rejects_stale_body_hash_pair() -> None:
    plan, _digest = _plan_with_valid_hash()
    stale = plan.model_copy(update={"harness_id": "harness-mutated"})

    with pytest.raises(ValueError, match="Computed hash"):
        canonical_compiled_plan_bytes(stale)


def test_canonical_builder_compiled_fixture_golden_shape_and_hashes() -> None:
    plan = make_canonical_builder_compiled_plan()
    raw = plan.model_dump_json()
    verified, computed, warnings, restored = verify_compiled_plan_sha256(
        raw,
        expected_compiled_hash=BUILDER_FIXTURE_COMPILED_SHA256,
        expected_harness_id=BUILDER_FIXTURE_HARNESS_ID,
        expected_harness_version=BUILDER_FIXTURE_HARNESS_VERSION,
    )

    assert verified is True
    assert warnings == []
    assert restored == plan
    assert computed == BUILDER_FIXTURE_COMPILED_SHA256
    assert plan.compiled_sha256 == BUILDER_FIXTURE_COMPILED_SHA256
    assert plan.source_sha256 == BUILDER_FIXTURE_PROMPT_SHA256
    assert builder_fixture_prompt_sha256() == BUILDER_FIXTURE_PROMPT_SHA256

    assert plan.harness_id == BUILDER_FIXTURE_HARNESS_ID
    assert plan.harness_version == 1
    assert plan.stage_kind_ids == ("builder",)
    assert plan.model_profile.profile_id == BUILDER_FIXTURE_PROFILE_ID
    assert plan.budgets.model_dump(mode="json") == {
        "max_iterations": 12,
        "max_validation_retries": 2,
        "max_tool_errors": 2,
        "max_prerequisite_violations": 2,
        "max_premature_terminal_attempts": 2,
    }
    assert plan.context_policy.model_dump(mode="json") == {
        "strategy_id": "forge.tiered.v1",
        "budget_tokens": 12000,
        "keep_recent_iterations": 2,
        "phase_thresholds": [0.60, 0.75, 0.90],
    }

    node_ids = tuple(node.node_id for node in plan.nodes)
    assert node_ids == (
        "inspect_request",
        "read_plan",
        "list_files",
        "read_file",
        "apply_patch",
        "read_diff",
        "run_validator",
        "write_patch_summary",
        "write_validation_results",
        "submit_patch",
        "block_builder",
    )
    assert tuple(node.model_tool_name for node in plan.nodes) == node_ids
    assert {node.node_id for node in plan.nodes if node.required} == {
        "inspect_request",
        "read_plan",
    }
    assert plan.terminal_result_map == {
        "submit_patch": "BUILDER_COMPLETE",
        "block_builder": "BUILDER_BLOCKED",
    }
    assert plan.required_capabilities == (
        "artifact.read",
        "workspace.read",
        "workspace.write",
        "shell.run",
        "artifact.write",
        "evidence.emit",
    )

    descriptor_hashes = {
        node.node_id: node.binding.descriptor_sha256 for node in plan.nodes
    }
    assert descriptor_hashes == BUILDER_FIXTURE_DESCRIPTOR_SHA256
    assert all(
        len(value) == 64 and value == value.lower()
        for value in descriptor_hashes.values()
    )
    assert all(
        set(value) <= set("0123456789abcdef") for value in descriptor_hashes.values()
    )

    schemas = {node.node_id: node.input_schema for node in plan.nodes}
    assert schemas == {
        "inspect_request": _closed_schema({}),
        "read_plan": _closed_schema({}),
        "list_files": _closed_schema({}),
        "read_file": _closed_schema(
            {"path": {"type": "string"}},
            required=("path",),
        ),
        "apply_patch": _closed_schema(
            {
                "path": {"type": "string"},
                "expected_text": {"type": "string"},
                "replacement_text": {"type": "string"},
            },
            required=("path", "expected_text", "replacement_text"),
        ),
        "read_diff": _closed_schema({}),
        "run_validator": _closed_schema(
            {"validator": {"const": "unit"}},
            required=("validator",),
        ),
        "write_patch_summary": _closed_schema(
            {
                "summary": {"type": "string"},
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            required=("summary", "changed_files"),
        ),
        "write_validation_results": _closed_schema(
            {
                "validator": {"const": "unit"},
                "passed": {"type": "boolean"},
                "summary": {"type": "string"},
            },
            required=("validator", "passed", "summary"),
        ),
        "submit_patch": _closed_schema(
            {
                "summary_artifact_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            required=("summary_artifact_ids",),
        ),
        "block_builder": _closed_schema(
            {
                "reason": {"type": "string"},
                "blocker_artifact_id": {"type": "string"},
            },
            required=("reason", "blocker_artifact_id"),
        ),
    }
    empty_schema_nodes = {
        node_id
        for node_id, schema in schemas.items()
        if schema["properties"] == {} and schema["required"] == []
    }
    assert empty_schema_nodes == {
        "inspect_request",
        "read_plan",
        "list_files",
        "read_diff",
    }


def test_canonical_builder_fixture_prerequisites_and_artifact_policy() -> None:
    plan = make_canonical_builder_compiled_plan()
    nodes = {node.node_id: node for node in plan.nodes}

    apply_prereq = nodes["apply_patch"].prerequisites[0]
    assert apply_prereq.node_id == "read_file"
    assert apply_prereq.argument_matches[0].model_dump(mode="json") == {
        "prerequisite_argument": "path",
        "current_argument": "path",
    }
    assert tuple(prereq.node_id for prereq in nodes["read_diff"].prerequisites) == (
        "apply_patch",
    )
    assert tuple(prereq.node_id for prereq in nodes["run_validator"].prerequisites) == (
        "apply_patch",
    )
    assert tuple(
        prereq.node_id for prereq in nodes["write_patch_summary"].prerequisites
    ) == ("read_diff",)
    assert tuple(
        prereq.node_id for prereq in nodes["write_validation_results"].prerequisites
    ) == ("run_validator",)
    assert tuple(prereq.node_id for prereq in nodes["submit_patch"].prerequisites) == (
        "inspect_request",
        "read_plan",
        "read_diff",
        "run_validator",
        "write_patch_summary",
        "write_validation_results",
    )
    assert tuple(prereq.node_id for prereq in nodes["block_builder"].prerequisites) == (
        "inspect_request",
        "read_plan",
    )

    assert plan.artifact_policy.declared_artifact_ids == (
        "patch_summary.json",
        "validation_results.json",
        "workspace_diff",
        "blocker_report.json",
    )
    assert [
        requirement.model_dump(mode="json")
        for requirement in plan.artifact_policy.required_by_terminal
    ] == [
        {
            "terminal_result": "BUILDER_COMPLETE",
            "artifact_ids": [
                "patch_summary.json",
                "validation_results.json",
                "workspace_diff",
            ],
        },
        {
            "terminal_result": "BUILDER_BLOCKED",
            "artifact_ids": ["blocker_report.json"],
        },
    ]
    assert plan.validate_plan_invariants() == []


def test_canonical_builder_execution_request_round_trip_and_constraints(
    tmp_path: Path,
) -> None:
    plan = make_canonical_builder_compiled_plan()
    request = make_canonical_builder_execution_request(tmp_path, plan=plan)
    restored = HarnessExecutionRequest.model_validate_json(request.model_dump_json())

    assert restored == request
    assert request.request_id == "request-builder-001"
    assert request.run_id == "run-builder-001"
    assert request.work_item_id == "work-builder-001"
    assert request.stage == StageIdentity(
        plane="execution",
        node_id="builder",
        stage_kind_id="builder",
    )
    assert (
        request.compiled_harness.path
        == tmp_path / "run-builder-001" / "millforge" / "compiled_plan.json"
    )
    assert request.compiled_harness.expected_hash.digest == plan.compiled_sha256
    assert request.input_artifacts == (
        ArtifactRef(
            artifact_id="plan",
            path=Path("millforge") / "compiled_plan.json",
            content_type="application/json",
        ),
    )
    assert request.run_directory.path == tmp_path / "run-builder-001"
    assert request.timeout.timeout_seconds == 120
    assert request.timeout.deadline is None
    assert request.cancellation.cancellation_id == "cancel-builder-001"
    assert request.secret_refs == ()
    assert request.model_profile.profile_id == BUILDER_FIXTURE_PROFILE_ID

    grants = {
        grant.capability_id: grant.constraints
        for grant in request.capability_envelope.grants
    }
    assert grants == {
        "artifact.read": {"artifact_ids": ["plan"]},
        "workspace.read": {"allowed_paths": ["src/example.py"]},
        "workspace.write": {"allowed_paths": ["src/example.py"]},
        "shell.run": {
            "allowed_validators": ["unit"],
            "subprocess_allowed": False,
        },
        "artifact.write": {
            "allowed_artifact_ids": [
                "patch_summary.json",
                "validation_results.json",
                "workspace_diff",
                "blocker_report.json",
            ],
        },
        "evidence.emit": {
            "allowed_terminal_results": [
                "BUILDER_COMPLETE",
                "BUILDER_BLOCKED",
            ],
        },
    }
    serialized = request.model_dump_json()
    assert "raw_secret" not in serialized.lower()
    assert "DATABASE_PASSWORD" not in serialized


def test_compiled_plan_rejects_off_contract_aliases() -> None:
    payload = _plan().model_dump(mode="json")
    payload["plan_id"] = "old"
    with pytest.raises(ValidationError, match="extra"):
        CompiledHarnessPlan.model_validate(payload)

    with pytest.raises(ValidationError):
        CompiledModelProfile(profile_id="standard", model_id="gpt-4")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ToolBindingRef(tool_name="weather", binding_ref="latest")  # type: ignore[call-arg]


def test_compiled_plan_invariants_reject_invalid_references() -> None:
    terminal = _node(produced_artifact_ids=())
    with pytest.raises(ValidationError, match="no producer"):
        CompiledHarnessPlan(
            **{
                **_plan().model_dump(),
                "nodes": (terminal,),
            }
        )

    with pytest.raises(ValidationError, match="Terminal node"):
        CompiledHarnessPlan(
            **{
                **_plan().model_dump(),
                "nodes": (_node(required=True),),
            }
        )

    with pytest.raises(ValidationError, match="unknown node_id"):
        CompiledHarnessPlan(
            **{
                **_plan().model_dump(),
                "nodes": (
                    _node(
                        terminal_result=None,
                        required=True,
                        prerequisites=(
                            CompiledPrerequisite(
                                node_id="missing",
                                argument_matches=(
                                    ArgumentMatch(
                                        prerequisite_argument="result",
                                        current_argument="input",
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
                "terminal_result_map": {},
                "artifact_policy": CompiledArtifactPolicy(),
            }
        )


def test_deadline_remaining_and_closed_source() -> None:
    deadline = Deadline(
        started_monotonic=10.0,
        outer_deadline_monotonic=30.0,
        compiled_harness_deadline_monotonic=25.0,
        effective_deadline_monotonic=25.0,
        source="compiled_harness",
    )
    assert deadline.request_deadline_monotonic == 30.0
    assert deadline.remaining(lambda: 15.0) == 10.0
    assert deadline.remaining(lambda: 40.0) == 0.0
    assert (
        Deadline.from_deadlines(
            started_monotonic=10.0,
            request_deadline_monotonic=30.0,
            compiled_harness_deadline_monotonic=25.0,
        )
        == deadline
    )
    with pytest.raises(ValidationError):
        Deadline.model_validate(
            {
                "started_monotonic": 10.0,
                "outer_deadline_monotonic": 30.0,
                "effective_deadline_monotonic": 25.0,
                "source": "manual",
            }
        )
    with pytest.raises(ValidationError, match="must equal the smaller"):
        Deadline(
            started_monotonic=10.0,
            outer_deadline_monotonic=30.0,
            compiled_harness_deadline_monotonic=25.0,
            effective_deadline_monotonic=30.0,
            source="request_and_harness",
        )
    with pytest.raises(ValidationError, match="must equal outer deadline"):
        Deadline(
            started_monotonic=10.0,
            outer_deadline_monotonic=30.0,
            effective_deadline_monotonic=25.0,
            source="request",
        )


def test_diagnostic_metadata_and_token_usage_are_closed() -> None:
    token_usage = TokenUsage(
        input_tokens=2,
        output_tokens=3,
        total_tokens=5,
        provider_reported=True,
    )
    assert token_usage.total_tokens == 5

    with pytest.raises(ValidationError):
        TokenUsage(
            input_tokens=2,
            output_tokens=3,
            total_tokens=6,
            provider_reported=True,
        )
    with pytest.raises(ValidationError):
        TokenUsage.model_validate(
            {
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
                "provider_reported": True,
                "reasoning_tokens": 1,
            }
        )

    diagnostic = DiagnosticMetadata(
        error_code="E_BINDING",
        category="binding",
        message="Binding was rejected",
        retryable=False,
        origin="runtime",
        fields=(DiagnosticField(key="node_id", value="n1"),),
    )
    assert diagnostic.category == "binding"
    with pytest.raises(ValidationError, match="unique"):
        DiagnosticMetadata(
            error_code="E_DUP",
            category="internal",
            message="Duplicate fields",
            retryable=False,
            origin="runtime",
            fields=(
                DiagnosticField(key="same", value=1),
                DiagnosticField(key="same", value=2),
            ),
        )


def test_usage_metadata_rejects_top_level_token_counters() -> None:
    usage = UsageMetadata(
        model_calls=1,
        tool_calls=2,
        token_usage=TokenUsage(
            input_tokens=2,
            output_tokens=3,
            total_tokens=5,
            provider_reported=True,
        ),
    )
    assert usage.model_calls == 1
    assert usage.tool_calls == 2
    assert usage.token_usage is not None
    assert usage.token_usage.total_tokens == 5

    usage_without_provider_tokens = UsageMetadata(
        model_calls=0,
        tool_calls=0,
        token_usage=None,
    )
    assert usage_without_provider_tokens.token_usage is None

    with pytest.raises(ValidationError):
        UsageMetadata.model_validate(
            {
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
                "model_calls": 1,
                "tool_calls": 2,
                "token_usage": None,
            }
        )


def test_bridge_model_capability_requirements_are_exact_02c_shape() -> None:
    requirements = ModelCapabilityRequirements()

    assert requirements.model_dump() == {
        "tool_calls": True,
        "parallel_tool_calls": False,
        "structured_output": False,
        "reasoning_controls": False,
        "usage_reporting": False,
        "system_messages": True,
        "tool_result_messages": True,
    }
    with pytest.raises(ValidationError):
        ModelCapabilityRequirements(parallel_tool_calls=True)  # type: ignore[arg-type]


def test_sampling_request_exposes_canonical_nullable_override_record() -> None:
    assert SamplingRequest().model_dump(mode="json") == {
        "temperature": None,
        "top_p": None,
        "presence_penalty": None,
        "frequency_penalty": None,
        "seed": None,
        "stop": None,
        "reasoning_mode": None,
        "reasoning_effort": None,
    }
    overrides = SamplingRequest(
        temperature=0.2,
        top_p=0.9,
        presence_penalty=0.1,
        frequency_penalty=0.0,
        seed=42,
        stop=("END",),
        reasoning_mode="disabled",
        reasoning_effort="low",
    )
    assert overrides.seed == 42
    with pytest.raises(ValidationError, match="extra"):
        SamplingRequest(max_tokens=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        SamplingRequest(stop=("",))


def test_model_completion_request_only_allows_positive_output_token_override() -> None:
    assert (
        ModelCompletionRequest(
            request_id="req-1",
            run_id="run-1",
            model_profile_id="gpt-test",
            messages=(UserMessage(content="Hi"),),
            maximum_output_tokens_override=1,
            deadline=_deadline(),
            cancellation=_cancellation(),
        ).maximum_output_tokens_override
        == 1
    )
    with pytest.raises(ValidationError):
        ModelCompletionRequest(
            request_id="req-1",
            run_id="run-1",
            model_profile_id="gpt-test",
            messages=(UserMessage(content="Hi"),),
            maximum_output_tokens_override=0,
            deadline=_deadline(),
            cancellation=_cancellation(),
        )


def test_sanitized_metadata_is_closed_and_bounded() -> None:
    metadata = SanitizedMetadata(values={"status": "ok", "attempt": 1})
    assert metadata.values["status"] == "ok"

    with pytest.raises(ValidationError, match="extra"):
        SanitizedMetadata(values={}, raw={"secret": "no"})  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="too many"):
        SanitizedMetadata(values={f"k{i}": i for i in range(33)})
    with pytest.raises(ValidationError, match="too long"):
        SanitizedMetadata(values={"k": "x" * 2049})


def test_model_completion_request_uses_typed_messages_tools_and_pairing() -> None:
    request = ModelCompletionRequest(
        request_id="req-1",
        run_id="run-1",
        model_profile_id="gpt-test",
        messages=(
            SystemMessage(content="Follow instructions."),
            UserMessage(content="Use the tool."),
            AssistantMessage(
                tool_calls=(
                    ModelToolCall(
                        call_id="call-1",
                        name="weather",
                        arguments=ParsedToolArguments(value={"city": "London"}),
                    ),
                ),
            ),
            ToolResultMessage(
                tool_call_id="call-1",
                tool_name="weather",
                content="Sunny",
            ),
        ),
        tools=(
            ModelToolDefinition(
                name="weather",
                description="Get weather",
                input_schema={"type": "object", "additionalProperties": False},
            ),
        ),
        deadline=_deadline(),
        cancellation=_cancellation(),
    )

    assert request.tools[0].name == "weather"
    assistant_message = cast(AssistantMessage, request.messages[2])
    arguments = assistant_message.tool_calls[0].arguments
    assert isinstance(arguments, ParsedToolArguments)
    assert arguments.value == {"city": "London"}
    dumped = request.model_dump(mode="json")
    assert list(dumped["messages"][0]) == ["role", "content"]
    assert dumped["messages"][0]["role"] == "system"
    assert dumped["messages"][1]["role"] == "user"
    assert dumped["messages"][2]["role"] == "assistant"
    assert dumped["messages"][3]["role"] == "tool"
    assert "kind" not in dumped["messages"][0]
    assert request.model_dump(mode="json")["messages"][3]["tool_name"] == "weather"

    with pytest.raises(ValidationError, match="union_tag_not_found|role"):
        ModelCompletionRequest.model_validate(
            {
                "request_id": "req-1",
                "run_id": "run-1",
                "model_profile_id": "gpt-test",
                "messages": ({"kind": "user", "content": "old"},),
                "deadline": _deadline().model_dump(mode="json"),
                "cancellation": _cancellation().model_dump(mode="json"),
            }
        )
    with pytest.raises(ValidationError, match="extra"):
        UserMessage.model_validate({"role": "user", "kind": "user", "content": "old"})

    with pytest.raises(ValidationError, match="tool_name"):
        ToolResultMessage(tool_call_id="call-1", content="Sunny")  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="tool_name"):
        ToolResultMessage(tool_call_id="call-1", tool_name=" ", content="Sunny")

    with pytest.raises(ValidationError, match="tool name"):
        ModelCompletionRequest(
            request_id="req-1",
            run_id="run-1",
            model_profile_id="gpt-test",
            messages=(UserMessage(content="Hi"),),
            tools=(
                ModelToolDefinition(name="same", description="A", input_schema={}),
                ModelToolDefinition(name="same", description="B", input_schema={}),
            ),
            deadline=_deadline(),
            cancellation=_cancellation(),
        )
    with pytest.raises(ValidationError, match="unique"):
        ModelCompletionRequest(
            request_id="req-1",
            run_id="run-1",
            model_profile_id="gpt-test",
            messages=(
                AssistantMessage(
                    tool_calls=(
                        ModelToolCall(
                            call_id="dup",
                            name="a",
                            arguments=ParsedToolArguments(),
                        ),
                        ModelToolCall(
                            call_id="dup",
                            name="b",
                            arguments=ParsedToolArguments(),
                        ),
                    ),
                ),
            ),
            deadline=_deadline(),
            cancellation=_cancellation(),
        )
    with pytest.raises(ValidationError, match="no matching"):
        ModelCompletionRequest(
            request_id="req-1",
            run_id="run-1",
            model_profile_id="gpt-test",
            messages=(
                ToolResultMessage(
                    tool_call_id="missing",
                    tool_name="weather",
                    content="x",
                ),
            ),
            deadline=_deadline(),
            cancellation=_cancellation(),
        )
    with pytest.raises(ValidationError, match="tool_name"):
        ModelCompletionRequest(
            request_id="req-1",
            run_id="run-1",
            model_profile_id="gpt-test",
            messages=(
                AssistantMessage(
                    tool_calls=(
                        ModelToolCall(
                            call_id="call-1",
                            name="weather",
                            arguments=ParsedToolArguments(),
                        ),
                    ),
                ),
                ToolResultMessage(
                    tool_call_id="call-1",
                    tool_name="calendar",
                    content="x",
                ),
            ),
            deadline=_deadline(),
            cancellation=_cancellation(),
        )


def test_public_bridge_records_expose_exact_canonical_json_shapes() -> None:
    invalid_arguments = InvalidToolArguments(raw="not-json", error_code="invalid_json")
    request = ModelCompletionRequest(
        request_id="req-1",
        run_id="run-1",
        model_profile_id="gpt-test",
        messages=(
            SystemMessage(content="Follow instructions."),
            UserMessage(content="Use the tool."),
            AssistantMessage(
                content=None,
                tool_calls=(
                    ModelToolCall(
                        call_id="call-1",
                        name="weather",
                        arguments=invalid_arguments,
                    ),
                ),
            ),
            ToolResultMessage(
                tool_call_id="call-1",
                tool_name="weather",
                content="Sunny",
            ),
        ),
        tools=(
            ModelToolDefinition(
                name="weather",
                description="Get weather",
                input_schema={"type": "object"},
            ),
        ),
        deadline=_deadline(),
        cancellation=_cancellation(),
    )

    assert SystemMessage(content="sys").model_dump(mode="json") == {
        "role": "system",
        "content": "sys",
    }
    assert UserMessage(content="hi").model_dump(mode="json") == {
        "role": "user",
        "content": "hi",
    }
    assert AssistantMessage(content="ok").model_dump(mode="json") == {
        "role": "assistant",
        "content": "ok",
        "tool_calls": [],
    }
    assert ToolResultMessage(
        tool_call_id="call-1",
        tool_name="weather",
        content="Sunny",
    ).model_dump(mode="json") == {
        "role": "tool",
        "tool_call_id": "call-1",
        "tool_name": "weather",
        "content": "Sunny",
    }
    assert ModelToolDefinition(
        name="weather",
        description="Get weather",
        input_schema={"type": "object"},
    ).model_dump(mode="json") == {
        "name": "weather",
        "description": "Get weather",
        "input_schema": {"type": "object"},
    }
    assert invalid_arguments.model_dump(mode="json") == {
        "kind": "invalid",
        "raw": "not-json",
        "error_code": "invalid_json",
    }

    dumped = request.model_dump(mode="json")
    assert list(dumped) == [
        "request_id",
        "run_id",
        "model_profile_id",
        "messages",
        "tools",
        "required_capabilities",
        "sampling_overrides",
        "maximum_output_tokens_override",
        "request_options",
        "deadline",
        "cancellation",
        "secret_refs",
    ]
    assert dumped["messages"][2]["tool_calls"][0]["arguments"] == {
        "kind": "invalid",
        "raw": "not-json",
        "error_code": "invalid_json",
    }
    assert "stream" not in dumped
    assert "metadata" not in dumped


def test_public_bridge_records_reject_stale_public_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        SystemMessage.model_validate(
            {"role": "system", "content": "sys", "metadata": {"values": {}}}
        )
    with pytest.raises(ValidationError, match="extra"):
        UserMessage.model_validate(
            {"role": "user", "content": "hi", "metadata": {"values": {}}}
        )
    with pytest.raises(ValidationError, match="extra"):
        AssistantMessage.model_validate(
            {
                "role": "assistant",
                "content": "ok",
                "tool_calls": [],
                "metadata": {"values": {}},
            }
        )
    with pytest.raises(ValidationError, match="extra"):
        ToolResultMessage.model_validate(
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "tool_name": "weather",
                "content": "Sunny",
                "metadata": {"values": {}},
            }
        )
    with pytest.raises(ValidationError, match="extra"):
        ModelToolDefinition.model_validate(
            {
                "name": "weather",
                "description": "Get weather",
                "input_schema": {},
                "required_capabilities": ["network.read"],
            }
        )
    with pytest.raises(ValidationError, match="extra"):
        InvalidToolArguments.model_validate(
            {
                "kind": "invalid",
                "raw": "not-json",
                "error_code": "invalid_json",
                "metadata": {"values": {}},
            }
        )

    canonical = {
        "request_id": "req-1",
        "run_id": "run-1",
        "model_profile_id": "gpt-test",
        "messages": [UserMessage(content="hi").model_dump(mode="json")],
        "tools": [],
        "required_capabilities": ModelCapabilityRequirements().model_dump(mode="json"),
        "sampling_overrides": SamplingRequest().model_dump(mode="json"),
        "maximum_output_tokens_override": None,
        "deadline": _deadline().model_dump(mode="json"),
        "cancellation": _cancellation().model_dump(mode="json"),
        "secret_refs": [],
    }
    assert ModelCompletionRequest.model_validate(canonical).request_id == "req-1"
    for stale_key, stale_value in (("stream", False), ("metadata", {"values": {}})):
        with pytest.raises(ValidationError, match="extra"):
            ModelCompletionRequest.model_validate({**canonical, stale_key: stale_value})
    for required_key in ("deadline", "cancellation"):
        stale = dict(canonical)
        stale.pop(required_key)
        with pytest.raises(ValidationError, match="Field required"):
            ModelCompletionRequest.model_validate(stale)


def test_model_response_and_validated_tool_calls_are_typed() -> None:
    invalid_args = InvalidToolArguments(
        raw={"not": "valid"},
        error_code="E_ARGUMENTS",
    )
    response = ModelCompletionResponse(
        provider_request_id=None,
        model_id="gpt-test",
        message=AssistantMessage(
            tool_calls=(
                ModelToolCall(call_id="call-1", name="weather", arguments=invalid_args),
            ),
        ),
        finish_reason="tool_calls",
        provider_metadata=None,
    )
    assert response.provider_request_id is None
    assert response.provider_metadata is None
    assert isinstance(response.tool_calls[0].arguments, InvalidToolArguments)
    assert (
        response.model_dump(mode="json")["message"]["tool_calls"][0]["call_id"]
        == "call-1"
    )
    assert "id" not in response.model_dump(mode="json")["message"]["tool_calls"][0]
    assert (
        ModelCompletionResponse(
            model_id="gpt-test",
            message=AssistantMessage(content="cancelled"),
            finish_reason="cancelled",
        ).finish_reason
        == "cancelled"
    )
    assert (
        ModelCompletionResponse(
            model_id="gpt-test",
            message=AssistantMessage(content="unknown"),
            finish_reason="unknown",
        ).finish_reason
        == "unknown"
    )
    with pytest.raises(ValidationError):
        ModelCompletionResponse(
            model_id="gpt-test",
            message=AssistantMessage(content="error"),
            finish_reason="error",  # type: ignore[arg-type]
        )

    call = ValidatedToolCall(
        call_id="call-1",
        node_id="node-weather",
        binding=_binding(),
        arguments={"city": "London"},
    )
    assert call.arguments == {"city": "London"}
    assert call.model_dump(mode="json") == {
        "call_id": "call-1",
        "node_id": "node-weather",
        "binding": _binding().model_dump(mode="json"),
        "arguments": {"city": "London"},
    }
    with pytest.raises(ValidationError, match="extra"):
        ValidatedToolCall.model_validate(
            {
                "call_id": "call-1",
                "node_id": "node-weather",
                "binding": _binding().model_dump(mode="json"),
                "name": "weather",
                "arguments": {"kind": "parsed", "value": {"city": "London"}},
            }
        )


def test_old_arbiter_reported_public_bridge_shapes_are_rejected() -> None:
    message_request = {
        "request_id": "req-1",
        "run_id": "run-1",
        "model_profile_id": "gpt-test",
        "messages": [{"kind": "tool_result", "content": "old"}],
    }
    with pytest.raises(ValidationError, match="union_tag_not_found|role"):
        ModelCompletionRequest.model_validate(message_request)

    response = ModelCompletionResponse(
        model_id="gpt-test",
        message=AssistantMessage(content="ok"),
        finish_reason="stop",
    )
    assert response.model_dump(mode="json")["provider_request_id"] is None
    assert response.model_dump(mode="json")["provider_metadata"] is None
    assert "error" not in get_args(
        ModelCompletionResponse.model_fields["finish_reason"].annotation
    )

    with pytest.raises(ValidationError, match="extra"):
        ValidatedToolCall.model_validate(
            {
                "call_id": "call-1",
                "node_id": "node-weather",
                "binding": _binding().model_dump(mode="json"),
                "arguments": {"kind": "parsed", "value": {"city": "London"}},
                "name": "weather",
                "metadata": {},
            }
        )

    canonical_result = ToolExecutionResult(
        call_id="call-1",
        status=ToolExecutionStatus.SUCCESS,
        summary="ok",
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
        input_sha256=SHA_B,
        output_sha256=SHA_A,
        timing=TimingMetadata(started_at="start", completed_at="end", duration_ms=0.0),
    )
    with pytest.raises(ValidationError, match="extra"):
        ToolExecutionResult.model_validate(
            {
                **canonical_result.model_dump(mode="json"),
                "node_id": "node-weather",
                "binding": _binding().model_dump(mode="json"),
                "started_monotonic": 0.0,
                "completed_monotonic": 1.0,
                "duration_ms": 1000.0,
                "metadata": {},
            }
        )


def test_tool_execution_result_success_error_and_hash_invariants() -> None:
    success = ToolExecutionResult(
        call_id="call-1",
        status=ToolExecutionStatus.SUCCESS,
        summary="ok",
        structured_data={"temperature": 70},
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
        input_sha256=SHA_B,
        output_sha256=SHA_A,
        timing=TimingMetadata(
            started_at="2026-06-12T00:00:00Z",
            completed_at="2026-06-12T00:00:01Z",
            duration_ms=500.0,
        ),
    )
    assert success.status == ToolExecutionStatus.SUCCESS
    assert "success" not in success.model_dump(mode="json")
    assert "output" not in success.model_dump(mode="json")
    assert "error" not in success.model_dump(mode="json")
    assert "node_id" not in success.model_dump(mode="json")
    assert "binding" not in success.model_dump(mode="json")
    assert "started_monotonic" not in success.model_dump(mode="json")
    assert success.model_dump(mode="json")["timing"] == {
        "started_at": "2026-06-12T00:00:00Z",
        "completed_at": "2026-06-12T00:00:01Z",
        "duration_ms": 500.0,
    }
    assert not hasattr(success, "success")
    assert not hasattr(success, "output")
    assert not hasattr(success, "error")

    failure = ToolExecutionResult(
        call_id="call-2",
        status=ToolExecutionStatus.SOFT_FAILURE,
        summary="temporary failure",
        error_code="E_TOOL_TEMPORARY",
        retryable=True,
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_ABSENT,
        input_sha256=SHA_B,
        output_sha256=None,
        timing=TimingMetadata(
            started_at="2026-06-12T00:00:02Z",
            completed_at="2026-06-12T00:00:03Z",
            duration_ms=1000.0,
        ),
    )
    assert failure.retryable is True

    with pytest.raises(ValidationError, match="successful"):
        ToolExecutionResult(
            call_id="call-3",
            status=ToolExecutionStatus.SUCCESS,
            summary="ok",
            error_code="E_BAD",
            side_effect_class=SideEffectClass.READ_ONLY,
            idempotency=IdempotencyClass.IDEMPOTENT,
            side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
            input_sha256=SHA_B,
            timing=TimingMetadata(
                started_at="start", completed_at="end", duration_ms=0.0
            ),
        )
    with pytest.raises(ValidationError, match="require error_code"):
        ToolExecutionResult(
            call_id="call-4",
            status=ToolExecutionStatus.HARD_FAILURE,
            summary="failed",
            side_effect_class=SideEffectClass.READ_ONLY,
            idempotency=IdempotencyClass.IDEMPOTENT,
            side_effect_certainty=SideEffectCertainty.CONFIRMED_ABSENT,
            input_sha256=SHA_B,
            timing=TimingMetadata(
                started_at="start", completed_at="end", duration_ms=0.0
            ),
        )

    with pytest.raises(ValidationError, match="lowercase hex"):
        ToolExecutionResult(
            call_id="call-5",
            status=ToolExecutionStatus.SUCCESS,
            summary="ok",
            side_effect_class=SideEffectClass.READ_ONLY,
            idempotency=IdempotencyClass.IDEMPOTENT,
            side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
            input_sha256=SHA_B,
            output_sha256="bad",
            timing=TimingMetadata(
                started_at="start", completed_at="end", duration_ms=0.0
            ),
        )

    with pytest.raises(ValidationError, match="extra"):
        ToolExecutionResult.model_validate(
            {
                **success.model_dump(mode="json"),
                "node_id": "node-weather",
                "binding": _binding().model_dump(mode="json"),
                "started_monotonic": 0.0,
                "completed_monotonic": 0.0,
                "duration_ms": 0.0,
                "metadata": {},
            }
        )


def test_tool_execution_result_rejects_unsafe_unknown_mutation_retry() -> None:
    with pytest.raises(ValidationError, match="completion_unknown"):
        ToolExecutionResult(
            call_id="call-unsafe",
            status=ToolExecutionStatus.AMBIGUOUS,
            summary="mutation may have completed",
            error_code="completion_unknown",
            retryable=True,
            side_effect_class=SideEffectClass.WORKSPACE_WRITE,
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
            side_effect_certainty=SideEffectCertainty.COMPLETION_UNKNOWN,
            input_sha256=SHA_B,
            timing=TimingMetadata(
                started_at="start", completed_at="end", duration_ms=0.0
            ),
        )

    with pytest.raises(ValidationError, match="retry_allowed"):
        ToolExecutionResult(
            call_id="call-detail",
            status=ToolExecutionStatus.SOFT_FAILURE,
            summary="retryable failure",
            error_code="retryable_failure",
            retryable=True,
            side_effect_class=SideEffectClass.READ_ONLY,
            idempotency=IdempotencyClass.IDEMPOTENT,
            side_effect_certainty=SideEffectCertainty.CONFIRMED_ABSENT,
            side_effect_record=SideEffectRecord(
                certainty=SideEffectCertainty.CONFIRMED_ABSENT,
                detail_code="absent",
                summary="No side effect occurred",
                retry_allowed=False,
            ),
            input_sha256=SHA_B,
            timing=TimingMetadata(
                started_at="start", completed_at="end", duration_ms=0.0
            ),
        )


def test_public_bridge_exports_canonical_tool_definition_name_only() -> None:
    assert "ModelToolDefinition" in millforge.__all__
    assert millforge.ModelToolDefinition is ModelToolDefinition
    assert "ToolDefinition" not in millforge.__all__
    assert not hasattr(millforge, "ToolDefinition")


def test_session_event_and_tool_trace_round_trip() -> None:
    stage = _stage()
    event = SessionEvent(
        schema_version="1.0",
        sequence=1,
        occurred_at="2026-06-12T00:00:00Z",
        monotonic_offset_ms=10.5,
        event_type=SessionEventType.RUNTIME_VERIFIED,
        request_id="req-1",
        run_id="run-1",
        session_id="sess-1",
        stage=stage,
        node_id="terminal",
        model_turn=0,
        tool_call_id="call-1",
        code=None,
        fields=(DiagnosticField(key="capability", value="workspace.read"),),
    )
    assert event.code is None
    assert event.stage == stage
    assert (
        SessionEvent.model_validate(event.model_dump()).model_dump()
        == event.model_dump()
    )

    trace = ToolTraceRecord(
        schema_version="1.0",
        sequence=2,
        occurred_at="2026-06-12T00:00:01Z",
        monotonic_offset_ms=20.5,
        request_id="req-1",
        run_id="run-1",
        session_id="sess-1",
        stage=stage,
        node_id="terminal",
        model_turn=0,
        tool_call_id="call-1",
        model_tool_name="complete_success",
        binding=_binding(),
        input_sha256=SHA_B,
        prerequisite_decisions=(
            ToolTraceDecisionRecord(key="prereq", decision=ToolTraceDecision.ALLOWED),
        ),
        capability_decisions=(
            ToolTraceDecisionRecord(
                key="workspace.read", decision=ToolTraceDecision.ALLOWED
            ),
        ),
        execution_status=ToolExecutionStatus.SUCCESS,
        retryable=False,
        side_effect_class=ToolTraceSideEffectClass.READ_ONLY,
        idempotency=ToolTraceIdempotency.IDEMPOTENT,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
        output_sha256=SHA_C,
        duration_ms=2.0,
        summary="Tool completed",
    )
    assert trace.stage == stage
    assert (
        ToolTraceRecord.model_validate(trace.model_dump()).model_dump()
        == trace.model_dump()
    )
    with pytest.raises(ValidationError):
        ToolTraceRecord.model_validate({**trace.model_dump(), "trace_id": "old"})


def test_session_event_rejects_off_contract_shapes() -> None:
    event_payload = {
        "schema_version": "1.0",
        "sequence": 1,
        "occurred_at": "2026-06-12T00:00:00Z",
        "monotonic_offset_ms": 10.0,
        "event_type": SessionEventType.RUNTIME_VERIFIED,
        "request_id": "req-1",
        "run_id": "run-1",
        "session_id": "sess-1",
        "stage": _stage(),
        "node_id": "terminal",
        "model_turn": 0,
        "tool_call_id": "call-1",
        "code": None,
        "fields": (DiagnosticField(key="capability", value="workspace.read"),),
    }
    with pytest.raises(ValidationError):
        SessionEvent.model_validate({**event_payload, "sequence": 0})
    with pytest.raises(ValidationError):
        SessionEvent.model_validate({**event_payload, "stage": "builder"})
    with pytest.raises(ValidationError, match="extra"):
        SessionEvent.model_validate({**event_payload, "message": "off contract"})
    with pytest.raises(ValidationError, match="unique"):
        SessionEvent.model_validate(
            {
                **event_payload,
                "fields": (
                    DiagnosticField(key="duplicate", value=1),
                    DiagnosticField(key="duplicate", value=2),
                ),
            }
        )


def test_tool_trace_rejects_off_contract_shapes() -> None:
    trace_payload = {
        "schema_version": "1.0",
        "sequence": 1,
        "occurred_at": "2026-06-12T00:00:01Z",
        "monotonic_offset_ms": 20.0,
        "request_id": "req-1",
        "run_id": "run-1",
        "session_id": "sess-1",
        "stage": _stage(),
        "node_id": "terminal",
        "model_turn": 0,
        "tool_call_id": "call-1",
        "model_tool_name": "complete_success",
        "binding": _binding(),
        "input_sha256": SHA_B,
        "prerequisite_decisions": (
            ToolTraceDecisionRecord(key="prereq", decision=ToolTraceDecision.ALLOWED),
        ),
        "capability_decisions": (
            ToolTraceDecisionRecord(
                key="workspace.read", decision=ToolTraceDecision.ALLOWED
            ),
        ),
        "execution_status": ToolExecutionStatus.SUCCESS,
        "retryable": False,
        "side_effect_class": ToolTraceSideEffectClass.READ_ONLY,
        "idempotency": ToolTraceIdempotency.IDEMPOTENT,
        "side_effect_certainty": SideEffectCertainty.CONFIRMED_COMPLETE,
        "output_sha256": SHA_C,
        "duration_ms": 2.0,
        "summary": "Tool completed",
    }
    with pytest.raises(ValidationError):
        ToolTraceRecord.model_validate({**trace_payload, "sequence": 0})
    with pytest.raises(ValidationError):
        ToolTraceRecord.model_validate({**trace_payload, "stage": "builder"})
    with pytest.raises(ValidationError, match="unique"):
        ToolTraceRecord.model_validate(
            {
                **trace_payload,
                "capability_decisions": (
                    ToolTraceDecisionRecord(
                        key="duplicate", decision=ToolTraceDecision.ALLOWED
                    ),
                    ToolTraceDecisionRecord(
                        key="duplicate", decision=ToolTraceDecision.DENIED
                    ),
                ),
            }
        )
    with pytest.raises(ValidationError, match="completion_unknown"):
        ToolTraceRecord.model_validate(
            {
                **trace_payload,
                "execution_status": ToolExecutionStatus.AMBIGUOUS,
                "retryable": True,
                "side_effect_class": ToolTraceSideEffectClass.WORKSPACE_WRITE,
                "idempotency": ToolTraceIdempotency.NON_IDEMPOTENT,
                "side_effect_certainty": SideEffectCertainty.COMPLETION_UNKNOWN,
                "output_sha256": None,
            }
        )
    with pytest.raises(ValidationError, match="provided together"):
        ToolTraceRecord.model_validate(
            {
                **trace_payload,
                "side_effect_detail_code": "rolled_back",
            }
        )


def test_session_event_and_tool_trace_closed_values() -> None:
    assert {item.value for item in SessionEventType} == {
        "runtime_received",
        "runtime_verified",
        "backend_constructed",
        "binding_rejected",
        "compiled_harness_invalid",
        "backend_failed",
        "session_started",
        "workflow_constructed",
        "model_request_started",
        "model_request_completed",
        "model_request_failed",
        "correction_issued",
        "premature_terminal_rejected",
        "prerequisite_rejected",
        "tool_started",
        "tool_completed",
        "tool_failed",
        "context_compacted",
        "terminal_intent_accepted",
        "terminal_intent_rejected",
        "finalization_started",
        "finalization_completed",
        "finalization_failed",
        "budget_exhausted",
        "timed_out",
        "cancelled",
        "internal_failed",
    }
    assert {item.value for item in ToolExecutionStatus} == {
        "not_executed",
        "success",
        "soft_failure",
        "hard_failure",
        "cancelled",
        "timed_out",
        "ambiguous",
    }


def test_compiled_harness_node_enum_values_are_closed() -> None:
    assert CompiledHarnessNode.model_fields["side_effect_class"].annotation is (
        SideEffectClass
    )
    assert CompiledHarnessNode.model_fields["idempotency"].annotation is (
        IdempotencyClass
    )
    assert {item.value for item in SideEffectClass} == {
        "read_only",
        "artifact_write",
        "workspace_write",
        "process_execution",
        "network_read",
        "network_write",
        "terminal",
    }
    assert {item.value for item in IdempotencyClass} == {
        "idempotent",
        "idempotent_with_key",
        "non_idempotent",
        "unknown",
    }


def test_tool_trace_enum_values_are_separate_from_compiled_node_enums() -> None:
    assert ToolTraceRecord.model_fields["side_effect_class"].annotation is (
        ToolTraceSideEffectClass
    )
    assert (
        ToolTraceRecord.model_fields["idempotency"].annotation is ToolTraceIdempotency
    )
    assert ToolTraceSideEffectClass is not SideEffectClass
    assert ToolTraceIdempotency is not IdempotencyClass
    assert {item.value for item in ToolTraceSideEffectClass} == {
        "read_only",
        "artifact_write",
        "workspace_write",
        "process_execution",
        "network_read",
        "network_write",
        "terminal",
    }
    assert {item.value for item in ToolTraceIdempotency} == {
        "idempotent",
        "idempotent_with_key",
        "non_idempotent",
        "unknown",
    }
    assert {item.value for item in SideEffectCertainty} == {
        "not_attempted",
        "confirmed_absent",
        "confirmed_complete",
        "rolled_back",
        "completion_unknown",
    }


def test_timeout_origins_are_closed_while_timeout_result_class_stays_stable() -> None:
    assert ExecutionResultClass.TIMED_OUT.value == "timed_out"
    assert {item.value for item in TimeoutOrigin} == {
        "session_deadline",
        "model_connect_timeout",
        "model_read_timeout",
        "model_write_timeout",
        "model_pool_timeout",
        "tool_timeout",
        "backend_timeout",
        "artifact_finalization_timeout",
        "cleanup_timeout",
    }
    diagnostic = DiagnosticMetadata(
        error_code="E_TIMEOUT",
        category="timeout",
        message="Timed out",
        retryable=False,
        origin=TimeoutOrigin.SESSION_DEADLINE,
    )
    assert diagnostic.model_dump(mode="json")["origin"] == "session_deadline"


def test_side_effect_record_detail_allows_retry_context_to_be_decided_by_result() -> (
    None
):
    record = SideEffectRecord(
        certainty=SideEffectCertainty.ROLLED_BACK,
        detail_code="rolled_back",
        summary="Mutation was rolled back",
        retry_allowed=True,
    )
    assert record.certainty is SideEffectCertainty.ROLLED_BACK

    unknown = SideEffectRecord(
        certainty=SideEffectCertainty.COMPLETION_UNKNOWN,
        detail_code="unknown",
        summary="Mutation may have completed",
        retry_allowed=True,
    )
    assert unknown.retry_allowed is True


def test_artifact_manifest_entry_supports_failure_metadata() -> None:
    incomplete = millforge.ArtifactManifestEntry(
        artifact_id="diagnostic",
        path="millforge/diagnostic.json",
        media_type="application/json",
        byte_size=0,
        sha256_hex=SHA_A,
        complete=False,
        producer="test/v1",
        failure_code="artifact_finalization_timeout",
    )
    assert incomplete.failure_code == "artifact_finalization_timeout"

    with pytest.raises(ValidationError, match="incomplete"):
        millforge.ArtifactManifestEntry(
            artifact_id="diagnostic",
            path="millforge/diagnostic.json",
            media_type="application/json",
            byte_size=0,
            sha256_hex=SHA_A,
            complete=False,
            producer="test/v1",
        )
    with pytest.raises(ValidationError, match="complete artifacts"):
        millforge.ArtifactManifestEntry(
            artifact_id="diagnostic",
            path="millforge/diagnostic.json",
            media_type="application/json",
            byte_size=0,
            sha256_hex=SHA_A,
            complete=True,
            producer="test/v1",
            failure_code="unexpected",
        )


def test_redaction_policy_contract_is_bounded_and_exported() -> None:
    policy = RedactionPolicy()
    assert policy.max_depth == 8
    assert policy.max_collection_items == 64
    assert policy.max_string_length == 2048
    assert policy.max_total_bytes == 32768
    assert "authorization" in policy.sensitive_field_markers
    assert "RedactionPolicy" in millforge.__all__

    with pytest.raises(ValidationError, match="extra"):
        RedactionPolicy(raw_secret="no")  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="unique"):
        RedactionPolicy(sensitive_field_markers=("token", "TOKEN"))


def test_shared_redaction_policy_sanitizes_sentinel_corpus_without_repr() -> None:
    class SecretRepr:
        def __repr__(self) -> str:
            raise AssertionError("repr must not be called")

        def __str__(self) -> str:
            raise AssertionError("str must not be called")

    secret = "sk-secret-value"
    cause = RuntimeError("nested bearer sk-secret-value")
    parent = RuntimeError("provider body api_key=sk-secret-value")
    parent.__cause__ = cause
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    redacted = redact_diagnostic_mapping(
        {
            "Authorization": f"Bearer {secret}",
            "X-Api-Key": secret,
            "credential_url": "https://user:pass@example.test/v1?api_key=abc",
            "provider_error_body": f"provider failed with token={secret}",
            "tool_input": {"password": secret, "nested": [secret, secret]},
            "tool_output": f"result contains {secret}",
            "exception": parent,
            "cycle": cyclic,
            "unsafe_object": SecretRepr(),
        },
        secret_values=(secret,),
    )
    serialized = str(redacted)

    assert secret not in serialized
    assert "api_key=abc" not in serialized
    assert redacted["Authorization"] == "**redacted**"
    assert redacted["X-Api-Key"] == "**redacted**"
    assert redacted["tool_input"] == {
        "password": "**redacted**",
        "nested": ["**redacted**", "**redacted**"],
    }
    assert redacted["cycle"] == {"self": "[cycle]"}
    assert redacted["unsafe_object"] == (
        "<tests.test_contracts.test_shared_redaction_policy_sanitizes_sentinel_corpus_"
        "without_repr.<locals>.SecretRepr>"
    )


def test_shared_redaction_policy_bounds_depth_collection_string_and_total_bytes() -> (
    None
):
    policy = RedactionPolicy(
        max_depth=2,
        max_collection_items=2,
        max_string_length=8,
        max_total_bytes=4096,
    )

    assert redact_diagnostic_text("x" * 20, policy=policy) == "xxxxxxxx[truncated]"
    assert redact_diagnostic_value(
        {"a": {"b": {"c": "secret=too-deep"}}, "list": [1, 2, 3]},
        policy=policy,
    ) == {
        "a": {"b": "[max_depth]"},
        "list": ["[max_depth]", "[max_depth]", "[truncated]"],
    }
    assert redact_diagnostic_value(["a", "b", "c"], policy=policy) == [
        "a",
        "b",
        "[truncated]",
    ]


def test_harness_execution_result_invariants() -> None:
    timing = TimingMetadata(started_at="start", completed_at="end", duration_ms=1.0)
    terminal = TerminalIntent(
        request_id="req-1",
        run_id="run-1",
        stage=_stage(),
        terminal_node_id="terminal",
        terminal_result="success",
        disposition="success",
        summary="Done",
        artifact_refs=(),
    )
    result = HarnessExecutionResult(
        status=ExecutionStatus.COMPLETED,
        result_class=ExecutionResultClass.DOMAIN_TERMINAL,
        request_id="req-1",
        run_id="run-1",
        stage=_stage(),
        terminal_intent=terminal,
        compiled_harness=_compiled_ref(),
        timing=timing,
    )
    assert result.terminal_intent == terminal

    with pytest.raises(ValidationError, match="status=completed"):
        HarnessExecutionResult(
            status=ExecutionStatus.COMPLETED,
            result_class=ExecutionResultClass.BACKEND_FAILURE,
            request_id="req-1",
            run_id="run-1",
            stage=_stage(),
            compiled_harness=_compiled_ref(),
            timing=timing,
        )

    with pytest.raises(ValidationError, match="terminal_intent"):
        HarnessExecutionResult(
            status=ExecutionStatus.FAILED,
            result_class=ExecutionResultClass.BACKEND_FAILURE,
            request_id="req-1",
            run_id="run-1",
            stage=_stage(),
            terminal_intent=terminal,
            compiled_harness=_compiled_ref(),
            timing=timing,
        )


def test_public_models_are_frozen_and_forbid_extras() -> None:
    frozen_models = [
        CompiledHarnessPlan,
        SessionEvent,
        ToolTraceRecord,
        Deadline,
        DiagnosticMetadata,
        HarnessExecutionResult,
    ]
    for model in frozen_models:
        config = getattr(model, "model_config")
        assert config["frozen"] is True
        assert config["extra"] == "forbid"

    with pytest.raises(ValidationError):
        ArtifactRef(artifact_id="a", path=Path("/tmp/a"), old_path="/tmp/old")  # type: ignore[call-arg]
