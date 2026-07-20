"""Cross-version deterministic output coverage for supported Python runtimes."""

from __future__ import annotations

import datetime
import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest

from millforge import (
    ArtifactManifestArtifact,
    ArtifactManifestEntry,
    CancellationRef,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    CompiledHarnessRef,
    ExecutionResultClass,
    ExecutionStatus,
    HarnessExecutionRequest,
    HarnessExecutionResult,
    HarnessTaskInput,
    ModelProfileRef,
    RunDirRef,
    SelectedOutputAbsent,
    SelectedOutputPresent,
    SelectedOutputRequirement,
    StageIdentity,
    TerminalSelectedOutputRequirement,
    TerminalIntent,
    TerminalResultArtifact,
    TimeoutRef,
    TimingMetadata,
)
from millforge.base import composition
from millforge.base.identity import _build_invocation_evidence, describe_millforge_base
from millforge.base.options import MillforgeBaseOptions
from millforge.tools.pi_compat.process import PiCompatShellConfig
from tests.conftest import FakeCancellationResolver, make_canonical_builder_profile_a

FIXTURE = Path(__file__).parent / "fixtures" / "python_compatibility" / "v1.json"
ROOT = Path(__file__).parents[1]


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _compatibility_outputs(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    fixed_root = Path("/")
    monkeypatch.setattr(composition, "_require_supported_platform", lambda: None)
    monkeypatch.setattr(
        composition,
        "_resolved_absolute_path",
        lambda _path, _field_name: fixed_root,
    )
    monkeypatch.setattr(composition.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        composition,
        "resolve_pi_compat_shell",
        lambda: PiCompatShellConfig(executable="/bin/sh", arguments=("-c",)),
    )
    monkeypatch.setattr(
        composition,
        "_create_pi_compat_tool_executor_for_terminal_results",
        lambda *_args, **_kwargs: object(),
    )

    components = composition.create_millforge_base_components(
        model_profile=make_canonical_builder_profile_a(),
        cwd=fixed_root,
        home_directory=fixed_root,
        cancellation_resolver=FakeCancellationResolver(),
        options=MillforgeBaseOptions(load_context_files=False),
        prompt_date=datetime.date(2026, 7, 17),
    )
    descriptor = describe_millforge_base()
    evidence = _build_invocation_evidence(
        components,
        descriptor,
        request_id="request-python-compatibility-v1",
        run_id="run-python-compatibility-v1",
    )

    terminal = TerminalResultArtifact(
        schema_version="1.0",
        request_id="request-python-compatibility-v1",
        run_id="run-python-compatibility-v1",
        stage=StageIdentity(
            plane="execution",
            node_id="millforge-base",
            stage_kind_id="millforge_base",
        ),
        terminal_result="COMPLETE",
        result_class=ExecutionResultClass.DOMAIN_TERMINAL,
        summary_artifact_paths=("millforge/execution_summary.json",),
        compiled_harness_sha256=components.compiled_plan.compiled_sha256,
    )
    terminal_payload = terminal.model_dump(mode="json")
    terminal_bytes = _canonical_json_bytes(terminal_payload)
    manifest = ArtifactManifestArtifact(
        schema_version="1.0",
        request_id=terminal.request_id,
        run_id=terminal.run_id,
        artifacts=(
            ArtifactManifestEntry(
                artifact_id="terminal_result",
                path="millforge/terminal_result.json",
                media_type="application/json",
                byte_size=len(terminal_bytes),
                sha256_hex=hashlib.sha256(terminal_bytes).hexdigest(),
                complete=True,
                producer="millforge.runtime",
            ),
        ),
    )
    prompt_metadata = components.prompt.model_dump(
        mode="json", exclude={"system_instructions"}
    )
    context_metadata = components.context.model_dump(mode="json")
    selected_requirement = SelectedOutputRequirement(
        required=True,
        json_schema={
            "type": "object",
            "properties": {"answer": {"type": "integer"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    optional_selected_requirement = SelectedOutputRequirement(
        required=False,
        json_schema=selected_requirement.json_schema,
    )
    terminal_selected_requirement = TerminalSelectedOutputRequirement(
        terminal_result="COMPLETE",
        selected_output=selected_requirement,
    )
    optional_terminal_selected_requirement = TerminalSelectedOutputRequirement(
        terminal_result="COMPLETE",
        selected_output=optional_selected_requirement,
    )
    selected_stage = StageIdentity(
        plane="execution",
        node_id="millforge-base",
        stage_kind_id="millforge_base",
    )
    selected_request = HarnessExecutionRequest(
        request_id="request-python-compatibility-selected-v1",
        run_id="run-python-compatibility-selected-v1",
        work_item_id="work-python-compatibility-selected-v1",
        task=HarnessTaskInput(
            instruction="Pin selected-output compatibility contract shapes."
        ),
        stage=selected_stage,
        compiled_harness=CompiledHarnessRef(
            identity=CompiledHarnessIdentity(
                compiled_plan_id="compiled-python-compatibility-v1",
                harness_id=components.compiled_plan.harness_id,
                harness_version=components.compiled_plan.harness_version,
            ),
            path=fixed_root / "millforge" / "compiled_plan.json",
            expected_hash=CompiledHarnessHash(
                algorithm="sha256",
                digest=components.compiled_plan.compiled_sha256,
            ),
        ),
        capability_envelope=components.capability_envelope,
        input_artifacts=(),
        run_directory=RunDirRef(
            run_id="run-python-compatibility-selected-v1",
            path=fixed_root / "millforge-python-compatibility-selected-v1",
        ),
        timeout=TimeoutRef(timeout_seconds=60, deadline=None),
        cancellation=CancellationRef(
            cancellation_id="cancel-python-compatibility-selected-v1"
        ),
        secret_refs=(),
        model_profile=ModelProfileRef(profile_id=components.model_profile.profile_id),
        selected_output_requirements=(terminal_selected_requirement,),
    )
    selected_null = SelectedOutputPresent(value=None)
    selected_absent = SelectedOutputAbsent()
    selected_terminal_null = TerminalIntent(
        request_id=selected_request.request_id,
        run_id=selected_request.run_id,
        stage=selected_stage,
        terminal_node_id="complete",
        terminal_result="COMPLETE",
        disposition="success",
        summary="Selected JSON null admitted.",
        selected_output=selected_null,
        selected_output_schema_sha256=selected_requirement.schema_sha256,
    )
    selected_terminal_absent = selected_terminal_null.model_copy(
        update={
            "summary": "Optional selected output absent.",
            "selected_output": selected_absent,
        }
    )
    selected_result_null = HarnessExecutionResult(
        status=ExecutionStatus.COMPLETED,
        result_class=ExecutionResultClass.DOMAIN_TERMINAL,
        request_id=selected_request.request_id,
        run_id=selected_request.run_id,
        stage=selected_stage,
        terminal_intent=selected_terminal_null,
        compiled_harness=selected_request.compiled_harness,
        timing=TimingMetadata(
            started_at="2026-07-17T00:00:00Z",
            completed_at="2026-07-17T00:00:01Z",
            duration_ms=1.0,
        ),
        selected_output=selected_null,
        selected_output_schema_sha256=selected_requirement.schema_sha256,
    )
    selected_result_absent = selected_result_null.model_copy(
        update={
            "terminal_intent": selected_terminal_absent,
            "selected_output": selected_absent,
        }
    )
    selected_required_evidence = _build_invocation_evidence(
        components,
        descriptor,
        request_id="request-python-compatibility-v1",
        run_id="run-python-compatibility-v1",
        selected_output_requirements=(terminal_selected_requirement,),
    )
    selected_optional_evidence = _build_invocation_evidence(
        components,
        descriptor,
        request_id="request-python-compatibility-v1",
        run_id="run-python-compatibility-v1",
        selected_output_requirements=(optional_terminal_selected_requirement,),
    )

    return {
        "artifact_json_sha256": _sha256(manifest.model_dump(mode="json")),
        "base_context_metadata_sha256": _sha256(context_metadata),
        "base_prompt_metadata_sha256": _sha256(prompt_metadata),
        "canonical_compiled_plan_sha256": _sha256(
            components.compiled_plan.model_dump(mode="json")
        ),
        "compiled_plan_embedded_sha256": components.compiled_plan.compiled_sha256,
        "invocation_evidence_embedded_sha256": evidence.invocation_sha256,
        "invocation_evidence_sha256": _sha256(evidence.model_dump(mode="json")),
        "runner_descriptor_embedded_sha256": descriptor.descriptor_sha256,
        "runner_descriptor_sha256": _sha256(descriptor.model_dump(mode="json")),
        "selected_harness_execution_request_sha256": _sha256(
            selected_request.model_dump(mode="json")
        ),
        "selected_harness_execution_result_absent_sha256": _sha256(
            selected_result_absent.model_dump(mode="json")
        ),
        "selected_harness_execution_result_null_sha256": _sha256(
            selected_result_null.model_dump(mode="json")
        ),
        "selected_invocation_evidence_optional_embedded_sha256": (
            selected_optional_evidence.invocation_sha256
        ),
        "selected_invocation_evidence_optional_sha256": _sha256(
            selected_optional_evidence.model_dump(mode="json")
        ),
        "selected_invocation_evidence_required_embedded_sha256": (
            selected_required_evidence.invocation_sha256
        ),
        "selected_invocation_evidence_required_sha256": _sha256(
            selected_required_evidence.model_dump(mode="json")
        ),
        "selected_output_requirements_optional_sha256": cast(
            str,
            selected_optional_evidence.selected_output_requirements_sha256,
        ),
        "selected_output_requirements_required_sha256": cast(
            str,
            selected_required_evidence.selected_output_requirements_sha256,
        ),
        "selected_output_absent_sha256": _sha256(
            SelectedOutputAbsent().model_dump(mode="json")
        ),
        "selected_output_null_sha256": _sha256(
            SelectedOutputPresent(value=None).model_dump(mode="json")
        ),
        "selected_output_requirement_sha256": _sha256(
            selected_requirement.model_dump(mode="json")
        ),
        "selected_output_schema_sha256": selected_requirement.schema_sha256,
        "terminal_selected_output_requirement_sha256": _sha256(
            terminal_selected_requirement.model_dump(mode="json")
        ),
        "selected_terminal_intent_absent_sha256": _sha256(
            selected_terminal_absent.model_dump(mode="json")
        ),
        "selected_terminal_intent_null_sha256": _sha256(
            selected_terminal_null.model_dump(mode="json")
        ),
        "terminal_json_sha256": hashlib.sha256(terminal_bytes).hexdigest(),
        "tool_catalog_sha256": descriptor.tool_catalog_sha256,
    }


def test_frozen_outputs_match_python_compatibility_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert _compatibility_outputs(monkeypatch) == expected


def test_python_support_metadata_and_lock_are_aligned() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))

    assert project["requires-python"] == ">=3.11"
    assert lock["requires-python"] == ">=3.11"
    assert {
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    }.issubset(project["classifiers"])
    assert not any(
        "Windows" in classifier or "PyPy" in classifier
        for classifier in project["classifiers"]
    )
