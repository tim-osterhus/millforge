"""Default compiler service orchestration tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Literal, NamedTuple

import pytest
from pydantic import ValidationError

from millforge import (
    CompiledHarnessPlan,
    FileCompiledHarnessLoader,
    STANDARD_ARTIFACT_FILENAMES,
    verify_compiled_plan_sha256,
)
from millforge.compiler import (
    CompileStatus,
    CompilerPhase,
    HarnessCompiler,
    HarnessCompileRequest,
    HarnessCompileResult,
    ModelProfileCatalogLookup,
    ModelProfileCatalogSnapshot,
    PlanCommitCertainty,
    ToolCatalogLookup,
    ToolCatalogSnapshot,
    compile as compile_harness,
    compile_raw,
    diagnostics_output_path,
    request_identity_sha256,
)
from millforge.compiler.diagnostics import CompilerDiagnostic, DiagnosticSeverity
from millforge.compiler.requests import DiagnosticReportState
import millforge.compiler.service as compiler_service
from millforge.contracts import (
    ArtifactRef,
    CancellationRef,
    CapabilityEnvelope,
    CapabilityGrant,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    CompiledHarnessRef,
    ExecutionResultClass,
    ExecutionStatus,
    HarnessExecutionRequest,
    HarnessTaskInput,
    ModelProfileRef,
    RunDirRef,
    StageIdentity,
    TimeoutRef,
)
from millforge.runtime import DefaultHarnessRuntime
from millforge.testing import FakeGuardrailBackend
from tests.conftest import (
    FakeArtifactWriter,
    FakeCancellationResolver,
    FakeClock,
    make_test_guarded_session_result,
)
from tests.compiler.conftest import (
    StaticModelProfileCatalogSnapshot,
    StaticToolCatalogSnapshot,
    make_golden_model_profile_catalog_snapshot,
    make_golden_tool_catalog_snapshot,
    make_raw_tool_descriptor,
)

FIXTURES = Path(__file__).parent / "fixtures"


class RepresentativeServiceFixture(NamedTuple):
    name: str
    filename: str
    source_format: Literal["yaml", "json"]
    expected_harness_id: str
    legal_terminal_results: tuple[str, ...]
    source_document_sha256: str
    source_sha256: str
    compiled_sha256: str


REPRESENTATIVE_SERVICE_FIXTURES: tuple[RepresentativeServiceFixture, ...] = (
    RepresentativeServiceFixture(
        name="full",
        filename="golden_harness.yaml",
        source_format="yaml",
        expected_harness_id="millforge.test.golden.compiler.v1",
        legal_terminal_results=("BLOCKED", "BUILDER_COMPLETE"),
        source_document_sha256=(
            "b5b3af7ab011b595c62642fc2acb18f1d2608ac0f1f7aa954d663c07a1f397b6"
        ),
        source_sha256=(
            "300d4655196b4d421a85969d4d381c07df70de9f92e12ee695a2f06ff04487b1"
        ),
        compiled_sha256=(
            "1d65583fe8bd8379d95f889fe0e889d9ee28ada85d912db9188191eb73bddc52"
        ),
    ),
    RepresentativeServiceFixture(
        name="simple_success",
        filename="representative_simple_success.json",
        source_format="json",
        expected_harness_id="millforge.test.representative.simple.v1",
        legal_terminal_results=("BUILDER_COMPLETE",),
        source_document_sha256=(
            "02571dfdbc9b1cc2266dd9bd82901f8f7528afd645e176dc32d10fd654d5b388"
        ),
        source_sha256=(
            "cff26dee6692c2059385c24934042c51eb8719c638f578a6b869fca21cc627f2"
        ),
        compiled_sha256=(
            "29e604298ecf09500b092ff95a3ad96686a386c52bb2e3c4819a54d83454d78b"
        ),
    ),
    RepresentativeServiceFixture(
        name="blocked_artifact",
        filename="representative_blocked_artifact.yaml",
        source_format="yaml",
        expected_harness_id="millforge.test.representative.blocked.v1",
        legal_terminal_results=("BLOCKED",),
        source_document_sha256=(
            "d3bc48422c1450e803e2187d8200ca003c62c57e55d67becac5ef76de5e856ba"
        ),
        source_sha256=(
            "b1fcca877db492fa8fa5ad834e9507546bc033d42c21278d93b9ce8195665318"
        ),
        compiled_sha256=(
            "9a68e99b7ce3185bd6a88724f40c4875ce5f4bd3836b691824e3564f42f58f4c"
        ),
    ),
)


def _source_payload(*, tool_ref: str = "tools.echo@1") -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "kind": "millforge_harness",
        "harness_id": "millforge.test.service.v1",
        "harness_version": 1,
        "stage_scope": {"stage_kind_ids": ["builder"]},
        "model_profile_id": "profile.standard",
        "prompt": {
            "policy_id": "millforge.test.service.policy.v1",
            "system_instructions": "Compile deterministically.",
            "include_request_context": True,
        },
        "budgets": {
            "max_iterations": 4,
            "max_validation_retries": 1,
            "max_tool_errors": 1,
            "max_prerequisite_violations": 1,
            "max_premature_terminal_attempts": 1,
        },
        "context": {
            "strategy_id": "forge.tiered.v1",
            "budget_tokens": 4096,
            "keep_recent_iterations": 1,
            "phase_thresholds": [0.5, 0.75, 0.9],
        },
        "graph": {
            "nodes": {
                "done": {
                    "tool_ref": tool_ref,
                    "terminal_result": "BUILDER_COMPLETE",
                }
            }
        },
        "artifacts": {"declared_artifact_ids": [], "required_by_terminal": {}},
    }


def _write_source(tmp_path: Path, payload: dict[str, Any] | None = None) -> Path:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "harness.json").write_text(
        json.dumps(payload or _source_payload()),
        encoding="utf-8",
    )
    return source_root


def _request(tmp_path: Path, *, output_dir: str = "compiled") -> HarnessCompileRequest:
    source_root = _write_source(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    (output_root / "compiled").mkdir()
    return HarnessCompileRequest(
        request_id="request.service.v1",
        source_path="harness.json",
        source_root=str(source_root),
        source_format="json",
        output_dir=output_dir,
        output_root=str(output_root),
        expected_harness_id="millforge.test.service.v1",
        stage_kind_id="builder",
        legal_terminal_results=("BUILDER_COMPLETE",),
        capability_envelope=CapabilityEnvelope(
            grants=(CapabilityGrant(capability_id="workspace.read"),)
        ),
    )


def _representative_request(
    tmp_path: Path,
    *,
    filename: str,
    source_format: Literal["yaml", "json"],
    expected_harness_id: str,
    legal_terminal_results: tuple[str, ...],
) -> HarnessCompileRequest:
    source_root = tmp_path / f"source-{expected_harness_id.rsplit('.', 2)[-2]}"
    output_root = tmp_path / f"output-{expected_harness_id.rsplit('.', 2)[-2]}"
    source_root.mkdir()
    output_root.mkdir()
    (output_root / "compiled").mkdir()
    shutil.copyfile(FIXTURES / filename, source_root / filename)
    return HarnessCompileRequest(
        request_id=f"request.{expected_harness_id}.compile",
        source_path=filename,
        source_root=str(source_root),
        source_format=source_format,
        output_dir="compiled",
        output_root=str(output_root),
        expected_harness_id=expected_harness_id,
        stage_kind_id="builder",
        legal_terminal_results=legal_terminal_results,
        capability_envelope=CapabilityEnvelope(
            grants=(
                CapabilityGrant(capability_id="artifact.write"),
                CapabilityGrant(capability_id="diagnostics.write"),
                CapabilityGrant(capability_id="evidence.emit"),
                CapabilityGrant(capability_id="workspace.read"),
            )
        ),
    )


def _tool_catalog() -> StaticToolCatalogSnapshot:
    descriptor = make_raw_tool_descriptor(
        tool_id="tools.echo",
        implementation_id="impl.tools.echo.v1",
        model_tool_name="echo",
        produced_artifact_ids=(),
    )
    return StaticToolCatalogSnapshot(entries={("tools.echo", 1): descriptor})


def _model_catalog() -> StaticModelProfileCatalogSnapshot:
    return StaticModelProfileCatalogSnapshot(
        profiles={"profile.standard": {"profile_id": "profile.standard"}}
    )


def _assert_failed_compile_row(
    result: HarnessCompileResult,
    *,
    phase: CompilerPhase,
    diagnostic_codes: tuple[str, ...],
    diagnostics_state: DiagnosticReportState = DiagnosticReportState.ABSENT,
) -> None:
    assert result.status == CompileStatus.FAILED
    assert result.plan_commit_certainty == PlanCommitCertainty.ABSENT
    assert result.diagnostic_report_state == diagnostics_state
    assert result.failure_phase == phase
    assert result.compiled_plan_path is None
    assert result.compiled_sha256 is None
    assert result.diagnostics_path is None
    assert [diagnostic.code for diagnostic in result.diagnostics] == list(
        diagnostic_codes
    )


def _assert_pre_output_failure_row(
    result: HarnessCompileResult,
    request: HarnessCompileRequest,
    *,
    diagnostic_codes: tuple[str, ...],
    source_sha256_state: str,
    harness_id_state: str,
    expected_field_keys: tuple[str, ...],
    outside_sentinel: Path,
    secret: str = "super-secret-value-that-must-not-leak",
) -> None:
    _assert_failed_compile_row(
        result,
        phase=CompilerPhase.LOWERING,
        diagnostic_codes=diagnostic_codes,
    )
    assert result.source_document_sha256 is not None
    assert ("present" if result.source_sha256 is not None else "absent") == (
        source_sha256_state
    )
    assert ("present" if result.harness_id is not None else "absent") == (
        harness_id_state
    )
    assert result.compiled_plan_path is None
    assert result.compiled_sha256 is None
    assert result.diagnostics_path is None
    assert not list(Path(request.output_root, request.output_dir).glob("*.tmp"))
    output_entries = sorted(
        path.name for path in Path(request.output_root, request.output_dir).iterdir()
    )
    assert output_entries == ["existing-output.txt"]
    assert outside_sentinel.read_text(encoding="utf-8") == "outside unchanged\n"
    assert tuple(field.key for field in result.diagnostics[0].fields) == (
        expected_field_keys
    )
    serialized = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    assert secret not in serialized


def _prepare_existing_outputs(request: HarnessCompileRequest, tmp_path: Path) -> Path:
    Path(request.output_root, request.output_dir, "existing-output.txt").write_text(
        "preserved\n",
        encoding="utf-8",
    )
    outside_sentinel = tmp_path / "outside-output-root.txt"
    outside_sentinel.write_text("outside unchanged\n", encoding="utf-8")
    return outside_sentinel


class _FailingToolCatalog:
    @property
    def snapshot_id(self) -> str:
        raise AssertionError("catalog metadata should not be read")

    @property
    def snapshot_sha256(self) -> str:
        raise AssertionError("catalog metadata should not be read")

    def resolve_exact(self, tool_id: str, tool_version: int) -> ToolCatalogLookup:
        del tool_id, tool_version
        raise AssertionError("catalog lookup should not run")


class _FailingModelProfileCatalog:
    @property
    def snapshot_id(self) -> str:
        raise AssertionError("catalog metadata should not be read")

    @property
    def snapshot_sha256(self) -> str:
        raise AssertionError("catalog metadata should not be read")

    def resolve_exact(self, profile_id: str) -> ModelProfileCatalogLookup:
        del profile_id
        raise AssertionError("catalog lookup should not run")


def test_public_harness_compiler_boundary_accepts_typed_compile_request(
    tmp_path: Path,
) -> None:
    class _ServiceCompiler:
        def compile(
            self,
            request: HarnessCompileRequest,
            *,
            tool_catalog: ToolCatalogSnapshot,
            model_profile_catalog: ModelProfileCatalogSnapshot,
        ) -> HarnessCompileResult:
            return compiler_service.compile(
                request,
                tool_catalog=tool_catalog,
                model_profile_catalog=model_profile_catalog,
            )

    compiler: HarnessCompiler = _ServiceCompiler()

    result = compiler.compile(
        _request(tmp_path),
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )

    assert result.status == CompileStatus.COMMITTED


def test_compile_commits_plan_and_diagnostics_for_valid_request(tmp_path: Path) -> None:
    request = _request(tmp_path)

    result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )

    assert result.status == CompileStatus.COMMITTED
    assert result.compiled_plan_path is not None
    assert result.diagnostics_path is not None
    assert result.harness_id == "millforge.test.service.v1"
    assert result.source_document_sha256 is not None
    assert result.source_sha256 is not None
    assert Path(request.output_root, result.compiled_plan_path).is_file()
    assert Path(request.output_root, result.diagnostics_path).is_file()


@pytest.mark.asyncio
async def test_representative_fixture_outputs_verify_and_load_without_source(
    tmp_path: Path,
) -> None:
    for case in REPRESENTATIVE_SERVICE_FIXTURES:
        request = _representative_request(
            tmp_path,
            filename=case.filename,
            source_format=case.source_format,
            expected_harness_id=case.expected_harness_id,
            legal_terminal_results=case.legal_terminal_results,
        )

        result = compile_harness(
            request,
            tool_catalog=make_golden_tool_catalog_snapshot(),
            model_profile_catalog=make_golden_model_profile_catalog_snapshot(),
        )

        assert result.status == CompileStatus.COMMITTED
        assert result.compiled_plan_path is not None
        assert result.source_document_sha256 == case.source_document_sha256
        assert result.source_sha256 == case.source_sha256
        assert result.compiled_sha256 == case.compiled_sha256
        emitted_plan_path = Path(request.output_root, result.compiled_plan_path)
        verified, computed, warnings, restored = verify_compiled_plan_sha256(
            emitted_plan_path.read_text(encoding="utf-8"),
            expected_compiled_hash=case.compiled_sha256,
            expected_harness_id=case.expected_harness_id,
            expected_harness_version=1,
        )
        shutil.rmtree(request.source_root)
        loaded = await FileCompiledHarnessLoader().load(
            CompiledHarnessRef(
                identity=CompiledHarnessIdentity(
                    compiled_plan_id=emitted_plan_path.stem,
                    harness_id=case.expected_harness_id,
                    harness_version=1,
                ),
                path=emitted_plan_path,
                expected_hash=CompiledHarnessHash(
                    algorithm="sha256",
                    digest=case.compiled_sha256,
                ),
            )
        )

        assert verified is True
        assert computed == case.compiled_sha256
        assert warnings == []
        assert restored == loaded
        assert loaded.compiled_sha256 == case.compiled_sha256
        assert not Path(request.source_root).exists()


@pytest.mark.parametrize(
    "case", REPRESENTATIVE_SERVICE_FIXTURES, ids=lambda case: case.name
)
def test_representative_diagnostics_report_shape_uses_hashes_and_relative_paths(
    tmp_path: Path, case: RepresentativeServiceFixture
) -> None:
    request = _representative_request(
        tmp_path,
        filename=case.filename,
        source_format=case.source_format,
        expected_harness_id=case.expected_harness_id,
        legal_terminal_results=case.legal_terminal_results,
    )

    result = compile_harness(
        request,
        tool_catalog=make_golden_tool_catalog_snapshot(),
        model_profile_catalog=make_golden_model_profile_catalog_snapshot(),
    )

    assert result.status == CompileStatus.COMMITTED
    assert result.plan_commit_certainty == PlanCommitCertainty.COMMITTED
    assert result.diagnostic_report_state == DiagnosticReportState.COMMITTED
    assert result.failure_phase is None
    assert result.request_id == request.request_id
    assert result.request_identity_sha256 == request_identity_sha256(request)
    assert result.harness_id == case.expected_harness_id
    assert result.source_document_sha256 == case.source_document_sha256
    assert result.source_sha256 == case.source_sha256
    assert result.compiled_sha256 == case.compiled_sha256
    assert result.compiled_plan_path == (
        f"compiled/{case.expected_harness_id}@1.{case.compiled_sha256}.compiled.json"
    )
    assert result.diagnostics_path == diagnostics_output_path(
        request,
        harness_id=case.expected_harness_id,
        harness_version=1,
        compiled_sha256=case.compiled_sha256,
    )
    assert result.diagnostics == ()
    assert result.compiled_plan_path is not None
    assert result.diagnostics_path is not None
    persisted = json.loads(
        Path(request.output_root, result.diagnostics_path).read_text(encoding="utf-8")
    )
    assert set(persisted) == {
        "request_id",
        "status",
        "plan_commit_certainty",
        "diagnostic_report_state",
        "failure_phase",
        "source_document_sha256",
        "source_sha256",
        "request_identity_sha256",
        "harness_id",
        "compiled_plan_path",
        "compiled_sha256",
        "diagnostics_path",
        "diagnostics",
    }
    assert persisted == result.model_dump(mode="json")
    assert persisted["status"] == "committed"
    assert persisted["plan_commit_certainty"] == "committed"
    assert persisted["diagnostic_report_state"] == "committed"
    assert persisted["failure_phase"] is None
    assert persisted["diagnostics"] == []
    assert persisted["request_id"] == request.request_id
    assert persisted["request_identity_sha256"] == request_identity_sha256(request)
    assert persisted["harness_id"] == case.expected_harness_id
    assert persisted["source_document_sha256"] == case.source_document_sha256
    assert persisted["source_sha256"] == case.source_sha256
    assert persisted["compiled_sha256"] == case.compiled_sha256
    assert persisted["compiled_plan_path"] == result.compiled_plan_path
    assert persisted["diagnostics_path"] == result.diagnostics_path
    assert not Path(persisted["compiled_plan_path"]).is_absolute()
    assert not Path(persisted["diagnostics_path"]).is_absolute()
    serialized = json.dumps(persisted, sort_keys=True)
    for forbidden in (
        str(tmp_path),
        str(FIXTURES),
        "Preserve this first instruction",
        "Collect the minimum context.",
        "Finish only after the draft exists.",
        "Gather context before writing a failure report.",
        "Block with evidence when the report is ready.",
        "impl.tools.collect_context.v1",
        "1111111111111111111111111111111111111111111111111111111111111111",
        "api_key",
        "super-secret-value",
        "https://",
        "AWS_SECRET_ACCESS_KEY",
    ):
        assert forbidden not in serialized


def test_compiler_output_does_not_publish_runtime_artifact_files(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )

    assert result.status == CompileStatus.COMMITTED
    output_names = {
        path.name for path in Path(request.output_root, request.output_dir).iterdir()
    }
    runtime_artifact_names = {
        Path(filename).name for filename in STANDARD_ARTIFACT_FILENAMES.values()
    }
    assert output_names.isdisjoint(runtime_artifact_names)
    assert all(
        name.endswith((".compiled.json", ".diagnostics.json")) for name in output_names
    )


@pytest.mark.asyncio
async def test_file_loader_rejects_unsupported_compiled_plan_fields(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    compile_result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )
    assert compile_result.compiled_plan_path is not None
    assert compile_result.compiled_sha256 is not None
    emitted_plan_path = Path(request.output_root, compile_result.compiled_plan_path)
    payload = json.loads(emitted_plan_path.read_text(encoding="utf-8"))
    payload["unsupported_runtime_field"] = True
    unsupported_path = emitted_plan_path.with_name("unsupported.compiled.json")
    unsupported_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="extra"):
        await FileCompiledHarnessLoader().load(
            CompiledHarnessRef(
                identity=CompiledHarnessIdentity(
                    compiled_plan_id=unsupported_path.stem,
                    harness_id="millforge.test.service.v1",
                    harness_version=1,
                ),
                path=unsupported_path,
                expected_hash=CompiledHarnessHash(
                    algorithm="sha256",
                    digest=compile_result.compiled_sha256,
                ),
            )
        )


@pytest.mark.asyncio
async def test_emitted_compiled_bytes_load_and_pass_runtime_preflight_without_source(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    compile_result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )
    assert compile_result.status == CompileStatus.COMMITTED
    assert compile_result.compiled_plan_path is not None
    assert compile_result.compiled_sha256 is not None

    emitted_plan_path = Path(request.output_root, compile_result.compiled_plan_path)
    shutil.rmtree(request.source_root)
    assert not Path(request.source_root).exists()

    session_result = make_test_guarded_session_result(
        request_id="request.runtime.compat",
        run_id="run-runtime-compat",
        with_events=False,
        with_tool_trace=False,
    )
    assert session_result.terminal_intent is not None
    session_result = session_result.model_copy(
        update={
            "terminal_intent": session_result.terminal_intent.model_copy(
                update={
                    "terminal_node_id": "done",
                    "terminal_result": "BUILDER_COMPLETE",
                }
            )
        }
    )

    class _EchoSessionBackend(FakeGuardrailBackend):
        async def run_session(self, guarded_request: Any) -> Any:
            response = await super().run_session(guarded_request)
            terminal_intent = response.terminal_intent
            if terminal_intent is not None:
                terminal_intent = terminal_intent.model_copy(
                    update={
                        "request_id": guarded_request.execution_request.request_id,
                        "run_id": guarded_request.execution_request.run_id,
                    }
                )
            return response.model_copy(
                update={
                    "session_id": guarded_request.session_id,
                    "terminal_intent": terminal_intent,
                }
            )

    backend = _EchoSessionBackend(responses=[session_result])
    runtime_run_dir = tmp_path / "runtime-run"
    runtime_input_path = Path("millforge") / "input.json"
    runtime_input_target = runtime_run_dir / runtime_input_path
    runtime_input_target.parent.mkdir(parents=True, exist_ok=True)
    runtime_input_target.write_text('{"schema_version":"test"}\n', encoding="utf-8")
    runtime = DefaultHarnessRuntime(
        backend=backend,
        plan_loader=FileCompiledHarnessLoader(),
        artifact_writer=FakeArtifactWriter(),
        clock=FakeClock(),
        cancellation_resolver=FakeCancellationResolver(),
    )
    execution_request = HarnessExecutionRequest(
        request_id="request.runtime.compat",
        run_id="run-runtime-compat",
        work_item_id="task-runtime-compat",
        task=HarnessTaskInput(instruction="Complete the compiler runtime task."),
        stage=StageIdentity(
            plane="execution",
            node_id="builder",
            stage_kind_id="builder",
        ),
        compiled_harness=CompiledHarnessRef(
            identity=CompiledHarnessIdentity(
                compiled_plan_id=emitted_plan_path.stem,
                harness_id="millforge.test.service.v1",
                harness_version=1,
            ),
            path=emitted_plan_path,
            expected_hash=CompiledHarnessHash(
                algorithm="sha256",
                digest=compile_result.compiled_sha256,
            ),
        ),
        capability_envelope=CapabilityEnvelope(
            grants=(CapabilityGrant(capability_id="workspace.read"),)
        ),
        input_artifacts=(
            ArtifactRef(
                artifact_id="runtime-input",
                path=runtime_input_path,
                content_type="application/json",
            ),
        ),
        run_directory=RunDirRef(
            run_id="run-runtime-compat",
            path=runtime_run_dir,
        ),
        timeout=TimeoutRef(timeout_seconds=120, deadline=None),
        cancellation=CancellationRef(cancellation_id="cancel-runtime-compat"),
        secret_refs=(),
        model_profile=ModelProfileRef(profile_id="profile.standard"),
    )
    loaded_plan = await FileCompiledHarnessLoader().load(
        execution_request.compiled_harness
    )
    assert loaded_plan.harness_id == "millforge.test.service.v1"
    assert loaded_plan.compiled_sha256 == compile_result.compiled_sha256

    result = await runtime.execute(execution_request)

    assert result.status == ExecutionStatus.COMPLETED
    assert result.result_class == ExecutionResultClass.DOMAIN_TERMINAL
    assert len(backend.requests) == 1
    guarded_request = backend.requests[0]
    assert guarded_request.execution_request.compiled_harness.path == emitted_plan_path


@pytest.mark.asyncio
async def test_runtime_rejects_compiled_hash_mismatch_before_backend_activity(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    compile_result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )
    assert compile_result.status == CompileStatus.COMMITTED
    assert compile_result.compiled_plan_path is not None
    assert compile_result.compiled_sha256 is not None
    emitted_plan_path = Path(request.output_root, compile_result.compiled_plan_path)
    runtime_run_dir = tmp_path / "runtime-hash-mismatch"
    backend = FakeGuardrailBackend()
    runtime = DefaultHarnessRuntime(
        backend=backend,
        plan_loader=FileCompiledHarnessLoader(),
        artifact_writer=FakeArtifactWriter(),
        clock=FakeClock(),
        cancellation_resolver=FakeCancellationResolver(),
    )

    result = await runtime.execute(
        HarnessExecutionRequest(
            request_id="request.runtime.hash_mismatch",
            run_id="run-runtime-hash-mismatch",
            work_item_id="task-runtime-compat",
            task=HarnessTaskInput(instruction="Complete the compiler runtime task."),
            stage=StageIdentity(
                plane="execution",
                node_id="builder",
                stage_kind_id="builder",
            ),
            compiled_harness=CompiledHarnessRef(
                identity=CompiledHarnessIdentity(
                    compiled_plan_id=emitted_plan_path.stem,
                    harness_id="millforge.test.service.v1",
                    harness_version=1,
                ),
                path=emitted_plan_path,
                expected_hash=CompiledHarnessHash(
                    algorithm="sha256",
                    digest="e" * 64,
                ),
            ),
            capability_envelope=CapabilityEnvelope(
                grants=(CapabilityGrant(capability_id="workspace.read"),)
            ),
            input_artifacts=(),
            run_directory=RunDirRef(
                run_id="run-runtime-hash-mismatch",
                path=runtime_run_dir,
            ),
            timeout=TimeoutRef(timeout_seconds=120, deadline=None),
            cancellation=CancellationRef(cancellation_id="cancel-runtime-compat"),
            secret_refs=(),
            model_profile=ModelProfileRef(profile_id="profile.standard"),
        )
    )

    assert result.status == ExecutionStatus.FAILED
    assert result.result_class == ExecutionResultClass.COMPILED_HARNESS_INVALID
    assert backend.requests == []


def test_compile_raw_frontend_failure_does_not_touch_catalogs(tmp_path: Path) -> None:
    request = _request(tmp_path).model_dump(mode="python")
    Path(str(request["source_root"]), str(request["source_path"])).write_text(
        "{not json",
        encoding="utf-8",
    )

    result = compile_raw(
        request,
        tool_catalog=_FailingToolCatalog(),
        model_profile_catalog=_FailingModelProfileCatalog(),
    )

    assert result.status == CompileStatus.FAILED
    assert result.failure_phase == CompilerPhase.PARSE
    assert result.diagnostics[0].code == "MF-S011"


def test_compile_request_validation_failure_returns_result_without_public_exception(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path).model_copy(update={"request_id": "not valid"})

    result = compile_harness(
        request,
        tool_catalog=_FailingToolCatalog(),
        model_profile_catalog=_FailingModelProfileCatalog(),
    )

    assert result.status == CompileStatus.FAILED
    assert result.request_id == "request.invalid"
    assert result.failure_phase == CompilerPhase.REQUEST
    assert result.diagnostics[0].code == "MF-S018"


def test_compile_request_admission_failure_does_not_touch_catalogs(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path).model_copy(update={"source_path": "../harness.json"})

    result = compile_harness(
        request,
        tool_catalog=_FailingToolCatalog(),
        model_profile_catalog=_FailingModelProfileCatalog(),
    )

    assert result.status == CompileStatus.FAILED
    assert result.failure_phase == CompilerPhase.REQUEST
    assert result.diagnostics[0].code == "MF-S001"


def test_compile_frontend_parse_failure_does_not_touch_catalogs(tmp_path: Path) -> None:
    request = _request(tmp_path)
    Path(request.source_root, request.source_path).write_text(
        "{not json",
        encoding="utf-8",
    )

    result = compile_harness(
        request,
        tool_catalog=_FailingToolCatalog(),
        model_profile_catalog=_FailingModelProfileCatalog(),
    )

    assert result.status == CompileStatus.FAILED
    assert result.failure_phase == CompilerPhase.PARSE
    assert result.diagnostics[0].code == "MF-S011"


def test_semantic_failure_returns_diagnostics_without_public_exception(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    result = compile_harness(
        request,
        tool_catalog=StaticToolCatalogSnapshot(entries={}),
        model_profile_catalog=_model_catalog(),
    )

    assert result.status == CompileStatus.FAILED
    assert result.failure_phase == CompilerPhase.RESOLUTION
    assert [diagnostic.code for diagnostic in result.diagnostics] == ["MF-R002"]
    assert result.compiled_plan_path is None


def test_service_reports_capability_before_dependent_artifact_failure(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    payload = _source_payload()
    payload["artifacts"] = {
        "declared_artifact_ids": ["report"],
        "required_by_terminal": {"BUILDER_COMPLETE": ["report"]},
    }
    Path(request.source_root, request.source_path).write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    descriptor = make_raw_tool_descriptor(
        tool_id="tools.echo",
        implementation_id="impl.tools.echo.v1",
        model_tool_name="echo",
        required_capabilities=("artifact.write",),
        produced_artifact_ids=(),
    )

    result = compile_harness(
        request,
        tool_catalog=StaticToolCatalogSnapshot(entries={("tools.echo", 1): descriptor}),
        model_profile_catalog=_model_catalog(),
    )

    assert result.status == CompileStatus.FAILED
    assert result.failure_phase == CompilerPhase.CAPABILITY
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "MF-C001",
        "MF-A004",
    ]
    assert result.compiled_plan_path is None


def test_output_failure_is_reported_as_output_diagnostic(tmp_path: Path) -> None:
    request = _request(tmp_path, output_dir="missing")

    result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )

    assert result.status == CompileStatus.FAILED
    assert result.failure_phase == CompilerPhase.REQUEST
    assert result.diagnostics[0].code == "MF-S017"


def test_lowering_invariant_failure_before_accepted_plan_validation_returns_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    outside = _prepare_existing_outputs(request, tmp_path)

    def fail_node_lowering(_node: object) -> object:
        raise TypeError("token=super-secret-value-that-must-not-leak")

    monkeypatch.setattr("millforge.compiler.lowering._lower_node", fail_node_lowering)

    result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )

    _assert_pre_output_failure_row(
        result,
        request,
        diagnostic_codes=("MF-L001",),
        source_sha256_state="present",
        harness_id_state="present",
        expected_field_keys=("error_type",),
        outside_sentinel=outside,
    )


def test_source_semantic_payload_failure_before_serialization_returns_redacted_lowering_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    outside = _prepare_existing_outputs(request, tmp_path)

    def fail_payload(_resolved: object) -> object:
        raise TypeError("token=super-secret-value-that-must-not-leak")

    monkeypatch.setattr(
        "millforge.compiler.canonicalization.canonical_semantic_payload",
        fail_payload,
    )

    result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )

    _assert_pre_output_failure_row(
        result,
        diagnostic_codes=("MF-L003",),
        request=request,
        source_sha256_state="absent",
        harness_id_state="absent",
        expected_field_keys=("error_type",),
        outside_sentinel=outside,
    )


def test_source_semantic_serialization_failure_returns_redacted_lowering_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    outside = _prepare_existing_outputs(request, tmp_path)

    def fail_source_serialization(_payload: object) -> str:
        raise TypeError("token=super-secret-value-that-must-not-leak")

    monkeypatch.setattr(
        "millforge.compiler.canonicalization.canonical_json_serialize",
        fail_source_serialization,
    )

    result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )

    _assert_pre_output_failure_row(
        result,
        request=request,
        diagnostic_codes=("MF-L003",),
        source_sha256_state="absent",
        harness_id_state="absent",
        expected_field_keys=("error_type",),
        outside_sentinel=outside,
    )


def test_compiled_plan_validation_failure_returns_canonical_lowering_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    outside = _prepare_existing_outputs(request, tmp_path)

    def reject_compiled_plan_validation(
        **_kwargs: object,
    ) -> CompiledHarnessPlan:
        try:
            CompiledHarnessPlan.model_validate({})
        except ValidationError as exc:
            raise exc
        raise AssertionError("invalid compiled plan unexpectedly validated")

    monkeypatch.setattr(
        "millforge.compiler.lowering.CompiledHarnessPlan",
        reject_compiled_plan_validation,
    )

    result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )

    _assert_pre_output_failure_row(
        result,
        request=request,
        diagnostic_codes=("MF-L002",),
        source_sha256_state="present",
        harness_id_state="present",
        expected_field_keys=("error_type",),
        outside_sentinel=outside,
    )


def test_compiled_plan_finalization_failure_after_accepted_plan_validation_returns_lowering_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    outside = _prepare_existing_outputs(request, tmp_path)
    seen_plans: list[CompiledHarnessPlan] = []

    def fail_finalize(plan: CompiledHarnessPlan) -> CompiledHarnessPlan:
        seen_plans.append(plan)
        assert plan.compiled_sha256 == "0" * 64
        assert plan.harness_id == request.expected_harness_id
        assert plan.source_sha256 is not None
        raise TypeError("token=super-secret-value-that-must-not-leak")

    monkeypatch.setattr(
        "millforge.compiler.lowering.finalize_compiled_plan_sha256",
        fail_finalize,
    )

    result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )

    assert len(seen_plans) == 1
    _assert_pre_output_failure_row(
        result,
        request=request,
        diagnostic_codes=("MF-L001",),
        source_sha256_state="present",
        harness_id_state="present",
        expected_field_keys=("error_type",),
        outside_sentinel=outside,
    )


def test_compiled_hash_serialization_exception_returns_lowering_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    outside = _prepare_existing_outputs(request, tmp_path)

    def fail_serialization(_payload: object) -> str:
        raise RuntimeError("token=super-secret-value-that-must-not-leak")

    monkeypatch.setattr(
        "millforge.compiler.service.canonical_json_serialize", fail_serialization
    )

    result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )

    _assert_pre_output_failure_row(
        result,
        request=request,
        diagnostic_codes=("MF-L004",),
        source_sha256_state="present",
        harness_id_state="present",
        expected_field_keys=("error_type",),
        outside_sentinel=outside,
    )


def test_compiled_hash_digest_mismatch_after_calculation_before_verifier_returns_lowering_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    outside = _prepare_existing_outputs(request, tmp_path)

    monkeypatch.setattr(
        "millforge.compiler.service.calculate_compiled_plan_sha256",
        lambda _payload: "d" * 64,
    )

    result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )

    _assert_pre_output_failure_row(
        result,
        request=request,
        diagnostic_codes=("MF-L004",),
        source_sha256_state="present",
        harness_id_state="present",
        expected_field_keys=("computed_hash",),
        outside_sentinel=outside,
    )


def test_compiled_hash_verifier_mismatch_returns_lowering_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)
    outside = _prepare_existing_outputs(request, tmp_path)

    def reject_hash(
        *_args: object, **_kwargs: object
    ) -> tuple[bool, str, list[str], None]:
        return False, "d" * 64, ["forced mismatch"], None

    monkeypatch.setattr(
        "millforge.compiler.service.verify_compiled_plan_sha256", reject_hash
    )

    result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )

    _assert_pre_output_failure_row(
        result,
        request=request,
        diagnostic_codes=("MF-L004",),
        source_sha256_state="present",
        harness_id_state="present",
        expected_field_keys=("computed_hash", "warning_count"),
        outside_sentinel=outside,
    )


def test_output_exception_before_publication_returns_internal_absent_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    request = _request(tmp_path)

    def fail_output(**_kwargs: object) -> object:
        raise OSError("pre-publication output failure")

    monkeypatch.setattr(
        "millforge.compiler.service.persist_compile_outputs", fail_output
    )

    result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )

    _assert_failed_compile_row(
        result,
        phase=CompilerPhase.INTERNAL,
        diagnostic_codes=("MF-I001",),
    )


def test_hash_calculation_diagnostic_boundary_is_preserved(
    tmp_path: Path, monkeypatch
) -> None:
    request = _request(tmp_path)
    diagnostic = CompilerDiagnostic(
        code="MF-L004",
        severity=DiagnosticSeverity.ERROR,
        phase=CompilerPhase.LOWERING,
        message="Forced hash calculation failure.",
    )

    monkeypatch.setattr(
        compiler_service,
        "_compiled_hash_failure",
        lambda _plan: diagnostic,
    )

    result = compile_harness(
        request,
        tool_catalog=_tool_catalog(),
        model_profile_catalog=_model_catalog(),
    )

    _assert_failed_compile_row(
        result,
        phase=CompilerPhase.LOWERING,
        diagnostic_codes=("MF-L004",),
    )
    assert result.diagnostics == (diagnostic,)


def test_compiler_service_uses_shared_compiled_plan_hash_helper() -> None:
    service_source = Path(compiler_service.__file__).read_text(encoding="utf-8")

    assert compiler_service.calculate_compiled_plan_sha256.__module__ == (
        "millforge.compiled_plan"
    )
    assert "def _calculate_compiled_plan_sha256" not in service_source
    assert "hashlib.sha256(canonical_json_serialize" not in service_source
