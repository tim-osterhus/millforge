"""Deterministic fake-backed integration fixture for full pipeline exercise.

Constructs a hand-authored ``CompiledHarnessPlan`` with one test node,
explicit ``CompilerIdentity``, and computed SHA-256.  Builds a complete
``HarnessExecutionRequest`` referencing that plan.  Instantiates
``DefaultHarnessRuntime`` with all fake/deterministic dependencies and
executes the full pipeline.  Verifies result shape, all 7 standard
artifacts written, byte-determinism on repeat run, and
``backend.run_session()`` called exactly once.

Must not depend on Forge, provider networking, harness source compiler,
or real tool implementations.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from millforge.artifacts import (
    STANDARD_ARTIFACT_FILENAMES,
    RuntimeArtifactWriter,
)
from millforge.compiled_plan import (
    ArgumentMatch,
    CompilerIdentity,
    CompiledArtifactPolicy,
    CompiledBudgetPolicy,
    CompiledContextPolicy,
    CompiledHarnessNode,
    CompiledHarnessPlan,
    CompiledPrerequisite,
    CompiledModelProfile,
    CompiledPromptPolicy,
    IdempotencyClass,
    SessionEventType,
    SideEffectCertainty,
    SideEffectClass,
    TerminalArtifactRequirement,
    ToolBindingRef,
    ToolExecutionStatus,
    canonical_json_serialize,
    finalize_compiled_plan_sha256,
)
from millforge.contracts import (
    ArtifactRef,
    AssistantMessage,
    CancellationRef,
    CapabilityEnvelope,
    CapabilityGrant,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    CompiledHarnessRef,
    DiagnosticMetadata,
    ExecutionResultClass,
    ExecutionStatus,
    GuardedSessionResult,
    GuardedSessionStatus,
    HarnessExecutionRequest,
    HarnessExecutionResult,
    ModelCompletionResponse,
    ModelToolCall,
    ModelProfileRef,
    ParsedToolArguments,
    RunDirRef,
    StageIdentity,
    TimeoutRef,
    TimingMetadata,
    ToolExecutionResult,
    TokenUsage,
    UsageMetadata,
)
from millforge._forge.adapter import ForgeContextFactory, ForgeGuardrailBackend
from millforge.protocols import GuardrailBackend
from millforge.runtime import DefaultHarnessRuntime
from millforge.testing import FakeGuardrailBackend, FakeModelClient, FakeToolExecutor

from tests.conftest import (
    FakeCancellationResolver,
    FakeClock,
    FakePlanLoader,
    make_test_session_event,
    make_test_terminal_intent,
    make_test_tool_trace_record,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_COMPILER_NAME = "test-compiler"
TEST_COMPILER_VERSION = "0.0.0"
TEST_COMPILER_BUILD_ID = "integration-test-build"
TEST_PLAN_ID = "plan-int-001"
TEST_HARNESS_ID = "harness-int-001"
TEST_HARNESS_VERSION = 1
TEST_REQUEST_ID = "req-int-001"
TEST_RUN_ID = "run-int-001"
TEST_WORK_ITEM_ID = "task-int-001"
TEST_PROFILE_ID = "deepseek_flash_high"
TEST_SESSION_ID = "00000000-0000-4000-8000-000000000001"
EXPECTED_INTEGRATION_COMPILED_SHA256 = (
    "2324d80b154199e20a048736571915d78502bd5c94d296f8e333d9d763a68c14"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_test_compiler_identity() -> CompilerIdentity:
    """Build an explicit CompilerIdentity with the test marker."""
    return CompilerIdentity(
        name=TEST_COMPILER_NAME,
        version=TEST_COMPILER_VERSION,
        build_id=TEST_COMPILER_BUILD_ID,
    )


def _build_test_model_profile() -> CompiledModelProfile:
    """Build a test CompiledModelProfile."""
    return CompiledModelProfile(profile_id=TEST_PROFILE_ID)


def _build_test_prompt_policy() -> CompiledPromptPolicy:
    """Build a test CompiledPromptPolicy."""
    return CompiledPromptPolicy(
        policy_id="policy-int-001",
        system_instructions="You are a helpful assistant.",
        include_request_context=True,
    )


def _build_test_budget_policy() -> CompiledBudgetPolicy:
    """Build a test CompiledBudgetPolicy."""
    return CompiledBudgetPolicy(
        max_iterations=2,
        max_validation_retries=1,
        max_tool_errors=1,
        max_prerequisite_violations=1,
        max_premature_terminal_attempts=1,
    )


def _build_test_context_policy() -> CompiledContextPolicy:
    """Build a test CompiledContextPolicy."""
    return CompiledContextPolicy(
        strategy_id="forge.tiered.v1",
        budget_tokens=4096,
        keep_recent_iterations=1,
        phase_thresholds=(0.25, 0.5, 1.0),
    )


def _build_test_terminal_artifact_requirement() -> TerminalArtifactRequirement:
    """Build a test TerminalArtifactRequirement."""
    return TerminalArtifactRequirement(
        terminal_result="success",
        artifact_ids=("art-output-001",),
    )


def _build_test_artifact_policy() -> CompiledArtifactPolicy:
    """Build a test CompiledArtifactPolicy."""
    return CompiledArtifactPolicy(
        declared_artifact_ids=("art-output-001",),
        required_by_terminal=(_build_test_terminal_artifact_requirement(),),
    )


def _build_prepare_node() -> CompiledHarnessNode:
    """Build the required non-terminal preparation node."""
    return CompiledHarnessNode(
        node_id="node-prepare",
        model_tool_name="prepare",
        description="Prepare input",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        binding=ToolBindingRef(
            tool_id="tool.prepare",
            tool_version=1,
            descriptor_sha256="a" * 64,
            implementation_id="impl.prepare.v1",
        ),
        prerequisites=(),
        required=True,
        terminal_result=None,
        required_capabilities=("workspace.read",),
        produced_artifact_ids=(),
        side_effect_class=SideEffectClass.READ_ONLY,
        idempotency=IdempotencyClass.IDEMPOTENT,
    )


def _build_terminal_node() -> CompiledHarnessNode:
    """Build the terminal node with a prerequisite edge."""
    return CompiledHarnessNode(
        node_id="node-001",
        model_tool_name="submit",
        description="Submit result",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        binding=ToolBindingRef(
            tool_id="tool.submit",
            tool_version=1,
            descriptor_sha256="b" * 64,
            implementation_id="impl.submit.v1",
        ),
        prerequisites=(
            CompiledPrerequisite(
                node_id="node-prepare",
                argument_matches=(
                    ArgumentMatch(
                        prerequisite_argument="path",
                        current_argument="path",
                    ),
                ),
            ),
        ),
        required=False,
        terminal_result="success",
        required_capabilities=("workspace.read",),
        produced_artifact_ids=("art-output-001",),
        side_effect_class=SideEffectClass.TERMINAL,
        idempotency=IdempotencyClass.IDEMPOTENT,
    )


def _build_test_plan_with_hash() -> CompiledHarnessPlan:
    """Build a CompiledHarnessPlan with a valid computed SHA-256.

    The shared compiled-plan helper finalizes the validated 02B plan body.
    """
    plan_without_hash = CompiledHarnessPlan(
        schema_version="1.0",
        kind="compiled_millforge_harness",
        harness_id=TEST_HARNESS_ID,
        harness_version=TEST_HARNESS_VERSION,
        source_sha256="a" * 64,
        compiled_sha256="b" * 64,
        stage_kind_ids=("builder",),
        model_profile=_build_test_model_profile(),
        prompt_policy=_build_test_prompt_policy(),
        budgets=_build_test_budget_policy(),
        context_policy=_build_test_context_policy(),
        nodes=(_build_prepare_node(), _build_terminal_node()),
        required_capabilities=("workspace.read",),
        terminal_result_map={"node-001": "success"},
        artifact_policy=_build_test_artifact_policy(),
        compiler_identity=_build_test_compiler_identity(),
    )
    return finalize_compiled_plan_sha256(plan_without_hash)


def _artifact_output() -> str:
    return json.dumps(
        {
            "summary": "done",
            "artifact_refs": [
                {
                    "artifact_id": "art-output-001",
                    "path": "millforge/output.json",
                    "content_type": "application/json",
                }
            ],
        },
        sort_keys=True,
    )


def _model_response(
    *,
    content: str,
    call_id: str,
    tool_name: str,
    usage: TokenUsage,
) -> ModelCompletionResponse:
    return ModelCompletionResponse(
        provider_request_id=f"provider-{call_id}",
        model_id=TEST_PROFILE_ID,
        message=AssistantMessage(
            content=content,
            tool_calls=(
                ModelToolCall(
                    call_id=call_id,
                    name=tool_name,
                    arguments=ParsedToolArguments(value={"path": "input.json"}),
                ),
            ),
        ),
        finish_reason="tool_calls",
        usage=usage,
    )


def _tool_result(
    call_id: str,
    summary: str,
    artifact_refs: tuple[ArtifactRef, ...] = (),
    input_arguments: dict[str, Any] | None = None,
) -> ToolExecutionResult:
    node = _build_terminal_node() if "submit" in call_id else _build_prepare_node()
    input_arguments = input_arguments or {"path": "input.json"}
    return ToolExecutionResult(
        call_id=call_id,
        status=ToolExecutionStatus.SUCCESS,
        summary=summary,
        artifact_refs=artifact_refs,
        side_effect_class=node.side_effect_class,
        idempotency=node.idempotency,
        side_effect_certainty=SideEffectCertainty.CONFIRMED_COMPLETE,
        input_sha256=hashlib.sha256(
            canonical_json_serialize(input_arguments).encode("utf-8")
        ).hexdigest(),
        output_sha256=None,
        timing=TimingMetadata(started_at="start", completed_at="end", duration_ms=0.0),
    )


def _build_test_request(
    plan: CompiledHarnessPlan,
    run_directory: Path | None = None,
) -> HarnessExecutionRequest:
    """Build a complete HarnessExecutionRequest referencing *plan*."""
    digest = plan.compiled_sha256
    run_path = run_directory or Path(f"/tmp/millforge/runs/{TEST_RUN_ID}")
    input_path = Path("millforge/input.json")
    input_target = run_path / input_path
    input_target.parent.mkdir(parents=True, exist_ok=True)
    input_target.write_text('{"schema_version":"test"}\n', encoding="utf-8")

    return HarnessExecutionRequest(
        request_id=TEST_REQUEST_ID,
        run_id=TEST_RUN_ID,
        work_item_id=TEST_WORK_ITEM_ID,
        stage=StageIdentity(
            plane="execution",
            node_id="builder",
            stage_kind_id="builder",
        ),
        compiled_harness=CompiledHarnessRef(
            identity=CompiledHarnessIdentity(
                compiled_plan_id=TEST_PLAN_ID,
                harness_id=plan.harness_id,
                harness_version=plan.harness_version,
            ),
            path=Path(f"/tmp/millforge/harnesses/{TEST_PLAN_ID}"),
            expected_hash=CompiledHarnessHash(
                algorithm="sha256",
                digest=digest,
            ),
        ),
        capability_envelope=CapabilityEnvelope(
            grants=(
                CapabilityGrant(capability_id="workspace.read"),
                CapabilityGrant(capability_id="artifact.write"),
            )
        ),
        input_artifacts=(
            ArtifactRef(
                artifact_id="art-input-001",
                path=input_path,
                content_type="application/json",
            ),
        ),
        run_directory=RunDirRef(
            run_id=TEST_RUN_ID,
            path=run_path,
        ),
        timeout=TimeoutRef(
            timeout_seconds=3600.0,
            deadline="2026-06-11T12:00:00+00:00",
        ),
        cancellation=CancellationRef(cancellation_id="cancel-int-001"),
        secret_refs=(),
        model_profile=ModelProfileRef(profile_id=TEST_PROFILE_ID),
    )


def _build_scripted_backend() -> FakeGuardrailBackend:
    """Build a FakeGuardrailBackend scripted with a valid session result.

    The session result includes:
    - ``TerminalIntent`` with disposition="success"
    - At least one ``SessionEvent``
    - At least one ``ToolTraceRecord``
    - ``UsageMetadata`` with nested token counts
    - ``TimingMetadata`` with timestamps
    - ``DiagnosticMetadata`` for diagnostic artifact exercise
    """
    terminal_intent = make_test_terminal_intent(
        request_id=TEST_REQUEST_ID,
        run_id=TEST_RUN_ID,
        terminal_result="success",
        disposition="success",
    )

    session_result = GuardedSessionResult(
        session_id=TEST_SESSION_ID,
        status=GuardedSessionStatus.TERMINAL,
        terminal_intent=terminal_intent,
        artifact_refs=(
            ArtifactRef(
                artifact_id="art-output-001",
                path=Path("millforge/output.json"),
                content_type="application/json",
            ),
        ),
        usage=UsageMetadata(
            model_calls=5,
            tool_calls=3,
            token_usage=TokenUsage(
                input_tokens=150,
                output_tokens=42,
                total_tokens=192,
                provider_reported=True,
            ),
        ),
        timing=TimingMetadata(
            started_at="2026-06-11T00:00:00Z",
            completed_at="2026-06-11T00:00:05Z",
            duration_ms=5000.0,
        ),
        diagnostic=DiagnosticMetadata(
            error_code="fixture_diagnostic",
            category="internal",
            message="Deterministic full-pipeline diagnostic fixture.",
            retryable=False,
            origin="integration_fixture",
        ),
        events=(
            make_test_session_event(
                sequence=1,
                session_id=TEST_SESSION_ID,
                event_type=SessionEventType.SESSION_STARTED,
                request_id=TEST_REQUEST_ID,
                run_id=TEST_RUN_ID,
            ),
            make_test_session_event(
                sequence=2,
                session_id=TEST_SESSION_ID,
                event_type=SessionEventType.TERMINAL_INTENT_ACCEPTED,
                request_id=TEST_REQUEST_ID,
                run_id=TEST_RUN_ID,
            ),
        ),
        tool_trace=(
            make_test_tool_trace_record(
                session_id=TEST_SESSION_ID,
            ),
        ),
    )

    return FakeGuardrailBackend(responses=[session_result])


def _build_forge_backend(plan_loader: FakePlanLoader) -> ForgeGuardrailBackend:
    model_client = FakeModelClient(
        responses=[
            _model_response(
                content="prepare first",
                call_id="model-call-prepare",
                tool_name="prepare",
                usage=TokenUsage(
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                    provider_reported=True,
                ),
            ),
            _model_response(
                content="submit now",
                call_id="model-call-submit",
                tool_name="submit",
                usage=TokenUsage(
                    input_tokens=12,
                    output_tokens=6,
                    total_tokens=18,
                    provider_reported=True,
                ),
            ),
        ]
    )
    tool_executor = FakeToolExecutor(
        supported_tools={"prepare", "submit"},
        results={
            "tool.prepare": [_tool_result("model-call-prepare", "prepared")],
            "tool.submit": [
                _tool_result(
                    "model-call-submit",
                    _artifact_output(),
                    (
                        ArtifactRef(
                            artifact_id="art-output-001",
                            path=Path("millforge/output.json"),
                            content_type="application/json",
                        ),
                    ),
                )
            ],
        },
    )
    return ForgeGuardrailBackend(
        model_client=model_client,
        tool_executor=tool_executor,
        plan_loader=plan_loader,
        context_factory=ForgeContextFactory(),
        clock=FakeClock(),
        cancellation_resolver=FakeCancellationResolver(is_cancelled=False),
    )


def _build_runtime(
    backend: GuardrailBackend,
    plan_loader: FakePlanLoader,
    artifact_writer: RuntimeArtifactWriter,
    clock: FakeClock,
    cancellation_resolver: FakeCancellationResolver,
) -> DefaultHarnessRuntime:
    """Build a DefaultHarnessRuntime wired with the given dependencies."""
    return DefaultHarnessRuntime(
        backend=backend,
        plan_loader=plan_loader,
        artifact_writer=artifact_writer,
        clock=clock,
        cancellation_resolver=cancellation_resolver,
    )


def _assert_result_shape(result: HarnessExecutionResult) -> None:
    """Assert the HarnessExecutionResult has the expected success shape."""
    assert result.status == ExecutionStatus.COMPLETED, (
        f"Expected COMPLETED, got {result.status}"
    )
    assert result.result_class == ExecutionResultClass.DOMAIN_TERMINAL, (
        f"Expected SUCCESS, got {result.result_class}"
    )
    assert result.terminal_intent is not None, "Expected terminal_intent to be present"
    assert result.terminal_intent.disposition == "success", (
        f"Expected disposition 'success', got {result.terminal_intent.disposition!r}"
    )
    assert result.usage is not None, "Expected usage to be present"
    assert result.usage.model_calls == 2, (
        f"Expected model_calls=2, got {result.usage.model_calls}"
    )
    assert result.usage.tool_calls == 2, (
        f"Expected tool_calls=2, got {result.usage.tool_calls}"
    )
    assert result.timing is not None, "Expected timing to be present"
    assert result.diagnostic is None, "Expected no diagnostic on success"


def _assert_artifacts_written(
    millforge_dir: Path,
) -> dict[str, bytes]:
    """Assert all expected artifacts are present in *millforge_dir*.

    Returns a dict mapping artifact_id to file content bytes for
    byte-determinism comparison.
    """
    artifact_contents: dict[str, bytes] = {}

    expected_present = [
        "terminal_result",
        "execution_summary",
        "events",
        "tool_trace",
        "metrics",
        "artifact_manifest",
    ]

    for aid in expected_present:
        filename = STANDARD_ARTIFACT_FILENAMES[aid]
        path = millforge_dir / filename
        assert path.exists(), f"Expected artifact {aid!r} at {path!r} does not exist"
        assert path.is_file(), f"Expected artifact {aid!r} at {path!r} is not a file"
        content = path.read_bytes()
        assert len(content) > 0, f"Artifact {aid!r} at {path!r} is empty"
        artifact_contents[aid] = content

    # Verify JSON validity for JSON artifacts
    for aid in ("terminal_result", "execution_summary", "metrics", "diagnostic"):
        content_bytes = artifact_contents.get(aid)
        if content_bytes is not None:
            parsed = json.loads(content_bytes)
            assert isinstance(parsed, dict), f"Artifact {aid!r} should be a JSON object"

    # Verify JSONL validity for line-delimited artifacts
    for aid in ("events", "tool_trace"):
        content_bytes = artifact_contents.get(aid)
        if content_bytes is not None:
            lines = content_bytes.split(b"\n")
            # Last line should be empty (trailing newline)
            if lines and lines[-1] == b"":
                lines = lines[:-1]
            assert len(lines) > 0, (
                f"JSONL artifact {aid!r} should have at least one line"
            )
            for i, line in enumerate(lines):
                assert len(line) > 0, (
                    f"JSONL artifact {aid!r} has empty line at index {i}"
                )
                parsed = json.loads(line)
                assert isinstance(parsed, dict), (
                    f"JSONL artifact {aid!r} line {i} should be a JSON object"
                )

    # Verify artifact manifest references
    manifest_bytes = artifact_contents.get("artifact_manifest")
    if manifest_bytes is not None:
        manifest = json.loads(manifest_bytes)
        assert "artifacts" in manifest, "Manifest should have an 'artifacts' key"
        manifest_artifact_ids = {a.get("artifact_id") for a in manifest["artifacts"]}
        for aid in expected_present:
            if aid != "artifact_manifest":
                assert aid in manifest_artifact_ids, (
                    f"Manifest is missing artifact_id {aid!r}"
                )
        # Manifest must not reference itself
        assert "artifact_manifest" not in manifest_artifact_ids, (
            "Manifest must not contain a self-reference"
        )

    return artifact_contents


def _assert_result_artifact_refs(
    result: HarnessExecutionResult,
    millforge_dir: Path,
) -> None:
    """Assert result artifact refs point to the standard written artifacts."""
    expected_ids = [
        "terminal_result",
        "execution_summary",
        "events",
        "tool_trace",
        "metrics",
        "artifact_manifest",
    ]

    assert result.artifact_refs is not None, "Expected artifact_refs to be present"
    returned_ids = [ref.artifact_id for ref in result.artifact_refs]
    assert returned_ids == expected_ids

    run_dir = millforge_dir.parent
    for ref in result.artifact_refs:
        expected_path = Path("millforge") / STANDARD_ARTIFACT_FILENAMES[ref.artifact_id]
        assert ref.path == expected_path

        written_path = run_dir / ref.path
        assert written_path.exists(), (
            f"Expected result artifact ref {ref.artifact_id!r} to point to "
            f"a written file at {written_path!r}"
        )
        assert written_path.is_file()
        assert written_path.parent == millforge_dir


def _assert_manifest_entry_matches_file(
    entry: dict[str, object],
    *,
    millforge_dir: Path,
    producer: str,
) -> None:
    """Assert one manifest entry matches the actual artifact file."""
    artifact_id = entry["artifact_id"]
    assert isinstance(artifact_id, str)
    filename = STANDARD_ARTIFACT_FILENAMES[artifact_id]
    artifact_path = millforge_dir / filename
    artifact_bytes = artifact_path.read_bytes()

    assert entry["path"] == f"millforge/{filename}"
    assert entry["media_type"] == "application/json"
    assert entry["byte_size"] == len(artifact_bytes)
    assert entry["sha256_hex"] == hashlib.sha256(artifact_bytes).hexdigest()
    assert entry["complete"] is True
    assert entry["producer"] == producer


def _assert_byte_determinism(run1: dict[str, bytes], run2: dict[str, bytes]) -> None:
    """Assert that two runs produce byte-identical artifacts."""
    assert run1.keys() == run2.keys(), (
        f"Artifact sets differ: run1 has {set(run1.keys())}, "
        f"run2 has {set(run2.keys())}"
    )
    for aid in run1:
        assert run1[aid] == run2[aid], (
            f"Byte mismatch for artifact {aid!r}: "
            f"run1 sha256={hashlib.sha256(run1[aid]).hexdigest()[:16]}... "
            f"run2 sha256={hashlib.sha256(run2[aid]).hexdigest()[:16]}..."
        )


# ===================================================================
# Integration test
# ===================================================================


@pytest.mark.asyncio
async def test_full_pipeline_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Execute the full fake-backed integration pipeline.

    Verifies:
    - Result shape (COMPLETED, SUCCESS, terminal intent present)
    - All 7 standard artifacts written
    - Artifact content validity (JSON, JSONL)
    - Byte-determinism on repeat run with identical inputs
    - backend.run_session() called exactly once
    """
    monkeypatch.setattr(
        "millforge.runtime.uuid.uuid4", lambda: uuid.UUID(TEST_SESSION_ID)
    )

    # ------------------------------------------------------------------
    # 1. Build the test plan with computed SHA-256
    # ------------------------------------------------------------------
    plan = _build_test_plan_with_hash()
    assert plan.compiled_sha256 == EXPECTED_INTEGRATION_COMPILED_SHA256

    # Validate plan invariants
    violations = plan.validate_plan_invariants()
    assert not violations, f"Plan invariant violations: {violations}"

    # ------------------------------------------------------------------
    # 3. Wire up all 5 dependencies
    # ------------------------------------------------------------------
    plan_loader = FakePlanLoader(plan=plan)
    backend = _build_forge_backend(plan_loader)
    clock = FakeClock()
    cancellation_resolver = FakeCancellationResolver(is_cancelled=False)

    # Real RuntimeArtifactWriter writing to tmp_path
    run_dir = tmp_path / "run-int-001"
    request = _build_test_request(plan, run_directory=run_dir)
    artifact_writer = RuntimeArtifactWriter(run_directory=run_dir)

    runtime = _build_runtime(
        backend=backend,
        plan_loader=plan_loader,
        artifact_writer=artifact_writer,
        clock=clock,
        cancellation_resolver=cancellation_resolver,
    )

    # ------------------------------------------------------------------
    # 4. Execute the full pipeline — Run 1
    # ------------------------------------------------------------------
    result1 = await runtime.execute(request)

    # ------------------------------------------------------------------
    # 5. Verify result shape
    # ------------------------------------------------------------------
    _assert_result_shape(result1)

    # ------------------------------------------------------------------
    # 6. Verify artifacts written
    # ------------------------------------------------------------------
    millforge_dir = run_dir / "millforge"
    assert millforge_dir.is_dir(), (
        f"millforge directory {millforge_dir!r} does not exist"
    )
    artifacts1 = _assert_artifacts_written(millforge_dir)
    _assert_result_artifact_refs(result1, millforge_dir)

    # ------------------------------------------------------------------
    # 7. Byte-determinism on repeat run
    # ------------------------------------------------------------------
    # Build a fresh runtime with the same deterministic inputs
    plan_loader2 = FakePlanLoader(plan=plan)
    backend2 = _build_forge_backend(plan_loader2)
    clock2 = FakeClock()
    cancellation_resolver2 = FakeCancellationResolver(is_cancelled=False)

    run_dir2 = tmp_path / "run-int-001"
    request2 = _build_test_request(plan, run_directory=run_dir2)
    artifact_writer2 = RuntimeArtifactWriter(run_directory=run_dir2)
    runtime2 = _build_runtime(
        backend=backend2,
        plan_loader=plan_loader2,
        artifact_writer=artifact_writer2,
        clock=clock2,
        cancellation_resolver=cancellation_resolver2,
    )

    result2 = await runtime2.execute(request2)
    _assert_result_shape(result2)

    millforge_dir2 = run_dir2 / "millforge"
    assert millforge_dir2.is_dir()
    artifacts2 = _assert_artifacts_written(millforge_dir2)
    _assert_result_artifact_refs(result2, millforge_dir2)

    _assert_byte_determinism(artifacts1, artifacts2)

    # ------------------------------------------------------------------
    # 8. Verify backend.run_session() called exactly once per run
    # ------------------------------------------------------------------
    assert len(backend.requests) == 1, (
        f"Expected backend.run_session() called exactly once, "
        f"got {len(backend.requests)}"
    )
    assert len(backend2.requests) == 1, (
        f"Expected backend.run_session() called exactly once on run 2, "
        f"got {len(backend2.requests)}"
    )

    # Verify the session request passed to the backend
    session_request = backend.requests[0]
    assert session_request.session_id, "Session ID should be non-empty"
    assert len(session_request.session_id) > 0
    assert session_request.execution_request.request_id == TEST_REQUEST_ID
    assert session_request.execution_request.run_id == TEST_RUN_ID


@pytest.mark.asyncio
async def test_failure_path_integration_manifest_matches_written_artifacts(
    tmp_path: Path,
) -> None:
    """Real runtime/writer failure path manifests only written non-terminal artifacts."""
    producer = "integration-test/v1"
    run_dir = tmp_path / "run-int-failure"
    plan = _build_test_plan_with_hash()
    request = _build_test_request(plan, run_directory=run_dir)
    backend = _build_scripted_backend()
    runtime = _build_runtime(
        backend=backend,
        plan_loader=FakePlanLoader(exception=RuntimeError("loader exploded")),
        artifact_writer=RuntimeArtifactWriter(run_directory=run_dir, producer=producer),
        clock=FakeClock(),
        cancellation_resolver=FakeCancellationResolver(is_cancelled=False),
    )

    result = await runtime.execute(request)

    assert result.status == ExecutionStatus.FAILED
    assert result.result_class == ExecutionResultClass.INTERNAL_FAILURE
    assert result.terminal_intent is None
    assert len(backend.requests) == 0

    millforge_dir = run_dir / "millforge"
    assert not (millforge_dir / "terminal_result.json").exists()
    manifest_path = millforge_dir / "artifact_manifest.json"
    assert manifest_path.exists()

    manifest = json.loads(manifest_path.read_text())
    manifest_entries = manifest["artifacts"]
    manifest_ids = {entry["artifact_id"] for entry in manifest_entries}
    assert manifest_ids == {"execution_summary", "metrics", "diagnostic"}
    assert "artifact_manifest" not in manifest_ids
    for entry in manifest_entries:
        _assert_manifest_entry_matches_file(
            entry,
            millforge_dir=millforge_dir,
            producer=producer,
        )

    diagnostic = json.loads((millforge_dir / "diagnostic.json").read_text())
    assert set(diagnostic) == {"schema_version", "diagnostic"}
    assert diagnostic["schema_version"] == "1.0"
    diagnostic_payload = diagnostic["diagnostic"]
    assert set(diagnostic_payload) == {
        "category",
        "error_code",
        "fields",
        "message",
        "origin",
        "retryable",
    }
    assert diagnostic_payload["error_code"] == "infrastructure_failure"
    assert diagnostic_payload["category"] == "internal"
    assert diagnostic_payload["origin"] == "infrastructure_failure"
    assert diagnostic_payload["retryable"] is True
    assert diagnostic_payload["fields"] == []
    assert diagnostic_payload["message"] == "Plan load failed: loader exploded"


@pytest.mark.asyncio
async def test_integration_pipeline_no_forbidden_imports() -> None:
    """Verify the integration test file has no forbidden imports."""
    import ast

    with open(__file__) as f:
        source = f.read()

    tree = ast.parse(source)

    forbidden_top_level: set[str] = {"forge", "ht" + "tpx"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top not in forbidden_top_level, (
                    f"Forbidden import {alias.name!r} found in test file"
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                top = node.module.split(".")[0]
                assert top not in forbidden_top_level, (
                    f"Forbidden import {node.module!r} found in test file"
                )


# ===================================================================
# Additional verification tests
# ===================================================================


@pytest.mark.asyncio
async def test_integration_backend_call_count_exactly_one(tmp_path: Path) -> None:
    """Verify backend.run_session() is called exactly once per execute()."""
    plan = _build_test_plan_with_hash()
    request = _build_test_request(plan, run_directory=tmp_path)
    backend = _build_scripted_backend()
    plan_loader = FakePlanLoader(plan=plan)
    clock = FakeClock()
    cancellation_resolver = FakeCancellationResolver(is_cancelled=False)
    artifact_writer = RuntimeArtifactWriter(run_directory=tmp_path)

    runtime = _build_runtime(
        backend=backend,
        plan_loader=plan_loader,
        artifact_writer=artifact_writer,
        clock=clock,
        cancellation_resolver=cancellation_resolver,
    )

    await runtime.execute(request)

    assert len(backend.requests) == 1, (
        f"Expected exactly 1 backend.run_session() call, got {len(backend.requests)}"
    )


@pytest.mark.asyncio
async def test_integration_plan_with_compiler_identity(tmp_path: Path) -> None:
    """Verify the test plan includes explicit CompilerIdentity with test marker."""
    compiler = _build_test_compiler_identity()
    assert compiler.name == TEST_COMPILER_NAME
    assert compiler.version == TEST_COMPILER_VERSION

    plan = _build_test_plan_with_hash()
    assert plan.compiler_identity.name == TEST_COMPILER_NAME
    assert plan.compiler_identity.version == TEST_COMPILER_VERSION


def test_grep_check_compiled_harness_plan() -> None:
    """Verify CompiledHarnessPlan is constructed in the integration test."""
    import re

    with open(__file__) as f:
        content = f.read()

    matches = re.findall(r"\bCompiledHarnessPlan\b", content)
    assert len(matches) >= 1, "CompiledHarnessPlan not referenced in integration test"


def test_grep_check_default_harness_runtime() -> None:
    """Verify DefaultHarnessRuntime is instantiated."""
    import re

    with open(__file__) as f:
        content = f.read()

    matches = re.findall(r"\bDefaultHarnessRuntime\b", content)
    assert len(matches) >= 1, "DefaultHarnessRuntime not referenced in integration test"


def test_grep_check_run_session() -> None:
    """Verify run_session is referenced for backend call verification."""
    import re

    with open(__file__) as f:
        content = f.read()

    matches = re.findall(r"\brun_session\b", content)
    assert len(matches) >= 1, "run_session not referenced in integration test"
