"""Resolved-harness to compiled-plan lowering tests."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, NamedTuple

import pytest
from millforge import (
    IdempotencyClass,
    CapabilityEnvelope,
    CapabilityGrant,
    CompiledHarnessPlan,
    CompiledModelProfile,
    SideEffectClass,
    canonical_compiled_plan_bytes,
    canonical_json_serialize,
    verify_compiled_plan_sha256,
)
from millforge.compiled_plan import calculate_compiled_plan_sha256
from millforge.compiler import (
    COMPILER_BUILD_ID,
    COMPILER_NAME,
    CompileInvocation,
    HarnessCompileRequest,
    HarnessSource,
    HarnessSourceParser,
    SourceDocument,
    compile_semantic,
    compiler_identity,
    lower_resolved_harness,
    source_sha256,
)
from tests.compiler.conftest import (
    StaticModelProfileCatalogSnapshot,
    StaticToolCatalogSnapshot,
    make_golden_compile_request,
    make_golden_model_profile_catalog_snapshot,
    make_golden_tool_catalog_snapshot,
    make_raw_tool_descriptor,
)

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED_GOLDEN_COMPILED_SHA256 = (
    "1d65583fe8bd8379d95f889fe0e889d9ee28ada85d912db9188191eb73bddc52"
)
EXPECTED_GOLDEN_COMPILED_BYTES_SHA256 = (
    "0149933a3971fa1fdec513e81b19b712fb38332a8a50dac561f5fc7101ddc610"
)


class RepresentativeCompiledFixture(NamedTuple):
    name: str
    yaml_filename: str
    json_filename: str
    harness_id: str
    legal_terminal_results: tuple[str, ...]
    source_sha256: str
    compiled_sha256: str
    compiled_byte_sha256: str
    compiled_byte_size: int
    stage_kind_ids: tuple[str, ...]
    node_ids: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    terminal_result_map: dict[str, str]
    declared_artifact_ids: tuple[str, ...]
    required_by_terminal: tuple[tuple[str, tuple[str, ...]], ...]
    phase_thresholds: tuple[float, ...]
    budgets: dict[str, int]


REPRESENTATIVE_COMPILED_FIXTURES: tuple[RepresentativeCompiledFixture, ...] = (
    RepresentativeCompiledFixture(
        name="full",
        yaml_filename="golden_harness.yaml",
        json_filename="golden_harness.json",
        harness_id="millforge.test.golden.compiler.v1",
        legal_terminal_results=("BLOCKED", "BUILDER_COMPLETE"),
        source_sha256="300d4655196b4d421a85969d4d381c07df70de9f92e12ee695a2f06ff04487b1",
        compiled_sha256=EXPECTED_GOLDEN_COMPILED_SHA256,
        compiled_byte_sha256=EXPECTED_GOLDEN_COMPILED_BYTES_SHA256,
        compiled_byte_size=4942,
        stage_kind_ids=("builder", "checker", "updater"),
        node_ids=(
            "blocked",
            "collect_context",
            "complete",
            "write_failure_report",
            "write_report",
        ),
        required_capabilities=(
            "artifact.write",
            "diagnostics.write",
            "evidence.emit",
            "workspace.read",
        ),
        terminal_result_map={
            "blocked": "BLOCKED",
            "complete": "BUILDER_COMPLETE",
        },
        declared_artifact_ids=("draft", "failure_report", "report"),
        required_by_terminal=(
            ("BLOCKED", ("failure_report",)),
            ("BUILDER_COMPLETE", ("draft", "report")),
        ),
        phase_thresholds=(0.5, 0.75, 0.95),
        budgets={
            "max_iterations": 6,
            "max_validation_retries": 2,
            "max_tool_errors": 2,
            "max_prerequisite_violations": 2,
            "max_premature_terminal_attempts": 2,
        },
    ),
    RepresentativeCompiledFixture(
        name="simple_success",
        yaml_filename="representative_simple_success.yaml",
        json_filename="representative_simple_success.json",
        harness_id="millforge.test.representative.simple.v1",
        legal_terminal_results=("BUILDER_COMPLETE",),
        source_sha256="cff26dee6692c2059385c24934042c51eb8719c638f578a6b869fca21cc627f2",
        compiled_sha256="29e604298ecf09500b092ff95a3ad96686a386c52bb2e3c4819a54d83454d78b",
        compiled_byte_sha256="90deb0edaf767a95a7bfa2080624abc7fdd19fffca34f4776accc7a1c4f9f82f",
        compiled_byte_size=2527,
        stage_kind_ids=("builder",),
        node_ids=("collect_context", "complete"),
        required_capabilities=("evidence.emit", "workspace.read"),
        terminal_result_map={"complete": "BUILDER_COMPLETE"},
        declared_artifact_ids=("draft",),
        required_by_terminal=(("BUILDER_COMPLETE", ("draft",)),),
        phase_thresholds=(0.4, 0.7, 0.9),
        budgets={
            "max_iterations": 3,
            "max_validation_retries": 1,
            "max_tool_errors": 1,
            "max_prerequisite_violations": 1,
            "max_premature_terminal_attempts": 1,
        },
    ),
    RepresentativeCompiledFixture(
        name="blocked_artifact",
        yaml_filename="representative_blocked_artifact.yaml",
        json_filename="representative_blocked_artifact.json",
        harness_id="millforge.test.representative.blocked.v1",
        legal_terminal_results=("BLOCKED",),
        source_sha256="b1fcca877db492fa8fa5ad834e9507546bc033d42c21278d93b9ce8195665318",
        compiled_sha256="9a68e99b7ce3185bd6a88724f40c4875ce5f4bd3836b691824e3564f42f58f4c",
        compiled_byte_sha256="45d16f1bbf8be7cfcae054deeac02f25bc6d08646dc6ed7f3688b91a5485e28e",
        compiled_byte_size=3322,
        stage_kind_ids=("builder", "checker"),
        node_ids=("blocked", "collect_context", "write_failure_report"),
        required_capabilities=(
            "artifact.write",
            "diagnostics.write",
            "evidence.emit",
            "workspace.read",
        ),
        terminal_result_map={"blocked": "BLOCKED"},
        declared_artifact_ids=("draft", "failure_report"),
        required_by_terminal=(("BLOCKED", ("failure_report",)),),
        phase_thresholds=(0.55, 0.8, 0.95),
        budgets={
            "max_iterations": 5,
            "max_validation_retries": 2,
            "max_tool_errors": 1,
            "max_prerequisite_violations": 2,
            "max_premature_terminal_attempts": 1,
        },
    ),
)


def _request() -> HarnessCompileRequest:
    return HarnessCompileRequest(
        request_id="request.lowering.v1",
        source_path="logical/harness.yaml",
        source_root="/tmp/source-root",
        source_format="yaml",
        output_dir="compiled",
        output_root="/tmp/output-root",
        expected_harness_id="millforge.test.lowering.v1",
        stage_kind_id="builder",
        legal_terminal_results=("BUILDER_COMPLETE",),
        capability_envelope=CapabilityEnvelope(
            grants=(
                CapabilityGrant(capability_id="artifact.write"),
                CapabilityGrant(capability_id="workspace.read"),
            )
        ),
    )


def _source(*, ordered: bool = True) -> HarnessSource:
    read_node: dict[str, Any] = {
        "tool_ref": "tools.read_file@1",
        "required": True,
        "produces": ["report", "draft"] if ordered else ["draft", "report"],
    }
    write_node: dict[str, Any] = {
        "tool_ref": "tools.write_report@1",
        "prerequisites": [
            {
                "node_id": "read_file",
                "argument_matches": (
                    {"beta": "beta", "alpha": "alpha"}
                    if ordered
                    else {"alpha": "alpha", "beta": "beta"}
                ),
            }
        ],
    }
    done_node: dict[str, Any] = {
        "tool_ref": "tools.done@1",
        "terminal_result": "BUILDER_COMPLETE",
        "prerequisites": [{"node_id": "write_report"}],
    }
    nodes = (
        {
            "read_file": read_node,
            "write_report": write_node,
            "done": done_node,
        }
        if ordered
        else {
            "done": done_node,
            "write_report": write_node,
            "read_file": read_node,
        }
    )
    return HarnessSource.model_validate(
        {
            "schema_version": "1.0",
            "kind": "millforge_harness",
            "harness_id": "millforge.test.lowering.v1",
            "harness_version": 1,
            "stage_scope": {
                "stage_kind_ids": ["builder", "checker"]
                if ordered
                else ["checker", "builder"]
            },
            "model_profile_id": "profile.standard",
            "prompt": {
                "policy_id": "millforge.test.policy.v1",
                "system_instructions": "Complete the request.",
                "include_request_context": True,
            },
            "budgets": {
                "max_iterations": 4,
                "max_validation_retries": 1,
                "max_tool_errors": 1,
                "max_prerequisite_violations": 1,
                "max_premature_terminal_attempts": 2,
            },
            "context": {
                "strategy_id": "forge.tiered.v1",
                "budget_tokens": 12000,
                "keep_recent_iterations": 1,
                "phase_thresholds": [0.6, 0.75, 0.9],
            },
            "graph": {"nodes": nodes},
            "artifacts": {
                "declared_artifact_ids": ["report", "draft"]
                if ordered
                else ["draft", "report"],
                "required_by_terminal": {
                    "BUILDER_COMPLETE": ["report", "draft"]
                    if ordered
                    else ["draft", "report"]
                },
            },
        }
    )


def _tool_snapshot(
    *,
    read_descriptor_update: Mapping[str, Any] | None = None,
    snapshot_id: str | None = None,
    snapshot_sha256: str | None = None,
) -> StaticToolCatalogSnapshot:
    read_descriptor = make_raw_tool_descriptor(
        tool_id="tools.read_file",
        implementation_id="impl.tools.read_file.v1",
        model_tool_name="read_file",
        input_schema={
            "type": "object",
            "properties": {
                "alpha": {"type": "string"},
                "beta": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["alpha", "beta", "path"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "alpha": {"type": "string"},
                "beta": {"type": "string"},
            },
            "required": ["alpha", "beta"],
            "additionalProperties": False,
        },
        required_capabilities=("workspace.read", "artifact.write"),
        produced_artifact_ids=("report", "draft"),
    )
    if read_descriptor_update is not None:
        read_descriptor = {**read_descriptor, **read_descriptor_update}
    return StaticToolCatalogSnapshot(
        entries={
            ("tools.read_file", 1): read_descriptor,
            ("tools.write_report", 1): make_raw_tool_descriptor(
                tool_id="tools.write_report",
                implementation_id="impl.tools.write_report.v1",
                model_tool_name="write_report",
                input_schema={
                    "type": "object",
                    "properties": {
                        "alpha": {"type": "string"},
                        "beta": {"type": "string"},
                    },
                    "required": ["alpha", "beta"],
                    "additionalProperties": False,
                },
                required_capabilities=("artifact.write",),
                produced_artifact_ids=(),
            ),
            ("tools.done", 1): make_raw_tool_descriptor(
                tool_id="tools.done",
                implementation_id="impl.tools.done.v1",
                model_tool_name="done",
                required_capabilities=(),
                produced_artifact_ids=(),
            ),
        },
        **({} if snapshot_id is None else {"snapshot_id": snapshot_id}),
        **({} if snapshot_sha256 is None else {"snapshot_sha256": snapshot_sha256}),
    )


def _model_snapshot() -> StaticModelProfileCatalogSnapshot:
    return StaticModelProfileCatalogSnapshot(
        profiles={
            "profile.standard": CompiledModelProfile(profile_id="profile.standard")
        }
    )


def _resolved(source: HarnessSource) -> Any:
    return _resolved_with(source, tool_snapshot=_tool_snapshot())


def _resolved_with(
    source: HarnessSource, *, tool_snapshot: StaticToolCatalogSnapshot
) -> Any:
    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        source,
        tool_snapshot=tool_snapshot,
        model_profile_snapshot=_model_snapshot(),
    )
    assert result.diagnostics == ()
    assert result.resolved_harness is not None
    return result.resolved_harness


def test_lower_resolved_harness_emits_accepted_compiled_plan_fields() -> None:
    resolved = _resolved(_source())
    plan = lower_resolved_harness(resolved)
    payload = plan.model_dump(mode="json")

    assert isinstance(plan, CompiledHarnessPlan)
    assert tuple(payload) == (
        "schema_version",
        "kind",
        "harness_id",
        "harness_version",
        "source_sha256",
        "compiled_sha256",
        "stage_kind_ids",
        "model_profile",
        "prompt_policy",
        "budgets",
        "context_policy",
        "nodes",
        "required_capabilities",
        "terminal_result_map",
        "artifact_policy",
        "compiler_identity",
    )
    assert plan.kind == "compiled_millforge_harness"
    assert plan.source_sha256 == source_sha256(resolved)
    assert plan.compiled_sha256 == calculate_compiled_plan_sha256(payload)
    assert plan.stage_kind_ids == ("builder", "checker")
    assert plan.required_capabilities == ("artifact.write", "workspace.read")
    assert plan.terminal_result_map == {"done": "BUILDER_COMPLETE"}
    assert plan.budgets.max_premature_terminal_attempts == 2
    assert plan.context_policy.strategy_id == "forge.tiered.v1"
    assert plan.context_policy.phase_thresholds == (0.6, 0.75, 0.9)

    read_node = plan.nodes[1]
    assert read_node.node_id == "read_file"
    assert read_node.model_tool_name == "read_file"
    assert read_node.binding.implementation_id == "impl.tools.read_file.v1"
    assert read_node.binding.descriptor_sha256 == "a" * 64
    assert read_node.produced_artifact_ids == ("draft", "report")
    assert read_node.side_effect_class == "read_only"
    assert read_node.idempotency == "idempotent"

    write_node = plan.nodes[2]
    assert write_node.prerequisites[0].argument_matches[0].prerequisite_argument == (
        "alpha"
    )
    assert write_node.prerequisites[0].argument_matches[0].current_argument == "alpha"


def test_lowering_compiler_identity_is_stable_and_uses_package_version() -> None:
    identity = compiler_identity()

    assert identity.name == COMPILER_NAME == "millforge"
    assert identity.version
    assert identity.build_id == COMPILER_BUILD_ID
    for forbidden in ("dirty", "/tmp", "pid", "host", "2026", "T"):
        assert forbidden not in identity.build_id


def test_lowered_plan_hash_verifies_and_ordering_is_deterministic() -> None:
    left = lower_resolved_harness(_resolved(_source(ordered=True)))
    right = lower_resolved_harness(_resolved(_source(ordered=False)))

    assert left.model_dump(mode="json") == right.model_dump(mode="json")
    verified, computed, warnings, restored = verify_compiled_plan_sha256(
        left.model_dump_json(),
        expected_compiled_hash=left.compiled_sha256,
        expected_harness_id=left.harness_id,
        expected_harness_version=left.harness_version,
    )
    assert verified is True
    assert warnings == []
    assert restored == left
    assert computed == left.compiled_sha256


def test_compiled_hash_matrix_tracks_source_and_represented_descriptor_changes() -> (
    None
):
    baseline = lower_resolved_harness(_resolved(_source()))

    source_changed = _source().model_copy(
        update={
            "prompt": _source().prompt.model_copy(
                update={"system_instructions": "Complete a different request."}
            )
        }
    )
    descriptor_cases: dict[str, Mapping[str, Any]] = {
        "descriptor_sha256": {"descriptor_sha256": "9" * 64},
        "implementation_id": {"implementation_id": "impl.tools.read_file.v2"},
        "model_tool_name": {"model_tool_name": "read_file_v2"},
        "description": {"description": "Read a file and emit normalized content."},
        "input_schema": {
            "input_schema": {
                "type": "object",
                "properties": {
                    "alpha": {"type": "string"},
                    "beta": {"type": "string"},
                    "path": {"type": "string"},
                    "mode": {"type": "string"},
                },
                "required": ["alpha", "beta", "path"],
                "additionalProperties": False,
            }
        },
        "capabilities": {"required_capabilities": ("workspace.read",)},
        "side_effect_class": {"side_effect_class": SideEffectClass.ARTIFACT_WRITE},
        "idempotency": {"idempotency": IdempotencyClass.IDEMPOTENT_WITH_KEY},
    }
    changed_hashes = {
        "source": lower_resolved_harness(_resolved(source_changed)).compiled_sha256
    }
    for case_name, update in descriptor_cases.items():
        plan = lower_resolved_harness(
            _resolved_with(
                _source(),
                tool_snapshot=_tool_snapshot(read_descriptor_update=update),
            )
        )
        changed_hashes[case_name] = plan.compiled_sha256

    metadata_changed = lower_resolved_harness(
        _resolved_with(
            _source(),
            tool_snapshot=_tool_snapshot(
                snapshot_id="d" * 64,
                snapshot_sha256="e" * 64,
            ),
        )
    )
    output_schema_only = lower_resolved_harness(
        _resolved_with(
            _source(),
            tool_snapshot=_tool_snapshot(
                read_descriptor_update={
                    "output_schema": {
                        "type": "object",
                        "properties": {
                            "alpha": {"type": "string"},
                            "beta": {"type": "string"},
                            "gamma": {"type": "string"},
                        },
                        "required": ["alpha", "beta"],
                        "additionalProperties": False,
                    }
                }
            ),
        )
    )
    output_schema_with_descriptor_hash = lower_resolved_harness(
        _resolved_with(
            _source(),
            tool_snapshot=_tool_snapshot(
                read_descriptor_update={
                    "descriptor_sha256": "8" * 64,
                    "output_schema": {
                        "type": "object",
                        "properties": {
                            "alpha": {"type": "string"},
                            "beta": {"type": "string"},
                            "gamma": {"type": "string"},
                        },
                        "required": ["alpha", "beta"],
                        "additionalProperties": False,
                    },
                }
            ),
        )
    )

    assert set(changed_hashes.values()).isdisjoint({baseline.compiled_sha256})
    assert metadata_changed.compiled_sha256 == baseline.compiled_sha256
    assert output_schema_only.compiled_sha256 == baseline.compiled_sha256
    assert output_schema_with_descriptor_hash.compiled_sha256 not in {
        baseline.compiled_sha256,
        output_schema_only.compiled_sha256,
    }
    assert "output_schema" not in canonical_json_serialize(
        baseline.model_dump(mode="json")
    )


def test_lowering_excludes_request_paths_catalog_metadata_and_catalog_objects() -> None:
    plan = lower_resolved_harness(_resolved(_source()))
    payload = plan.model_dump(mode="json")
    serialized = canonical_json_serialize(payload)

    for forbidden in (
        "request.lowering.v1",
        "/tmp/source-root",
        "/tmp/output-root",
        "logical/harness.yaml",
        "source_format",
        "snapshot_id",
        "snapshot_sha256",
        "source_root",
        "output_root",
    ):
        assert forbidden not in serialized

    read_node = cast_mapping(cast_list(payload["nodes"])[1])
    input_schema = cast_mapping(read_node["input_schema"])
    assert type(input_schema) is dict

    payload["harness_id"] = "mutated"
    assert plan.harness_id == "millforge.test.lowering.v1"


def test_lowered_plan_bytes_are_canonical_utf8_json_with_one_trailing_lf() -> None:
    plan = lower_resolved_harness(_resolved(_source()))
    compiled_bytes = canonical_compiled_plan_bytes(plan)

    assert compiled_bytes == canonical_json_serialize(
        plan.model_dump(mode="json")
    ).encode("utf-8")
    assert compiled_bytes.endswith(b"\n")
    assert not compiled_bytes.endswith(b"\n\n")
    assert not compiled_bytes.startswith(b"\xef\xbb\xbf")
    assert b"NaN" not in compiled_bytes
    assert b"Infinity" not in compiled_bytes


@pytest.mark.parametrize(
    "case", REPRESENTATIVE_COMPILED_FIXTURES, ids=lambda case: case.name
)
def test_representative_lowering_pins_final_plan_bytes_and_ordering(
    case: RepresentativeCompiledFixture,
) -> None:
    yaml_plan = lower_resolved_harness(
        _representative_resolved(case, case.yaml_filename, "yaml")
    )
    json_plan = lower_resolved_harness(
        _representative_resolved(case, case.json_filename, "json")
    )
    expected_plan_bytes = (
        (FIXTURES / "golden_compiled_plan.json").read_bytes()
        if case.name == "full"
        else canonical_compiled_plan_bytes(yaml_plan)
    )
    plan_bytes = canonical_compiled_plan_bytes(yaml_plan)

    assert yaml_plan == json_plan
    assert yaml_plan.source_sha256 == case.source_sha256
    assert yaml_plan.compiled_sha256 == case.compiled_sha256
    assert plan_bytes == expected_plan_bytes
    assert len(plan_bytes) == case.compiled_byte_size
    assert (
        calculate_compiled_plan_sha256(yaml_plan.model_dump(mode="json"))
        == case.compiled_sha256
    )
    assert hashlib.sha256(plan_bytes).hexdigest() == case.compiled_byte_sha256
    assert yaml_plan.stage_kind_ids == case.stage_kind_ids
    assert tuple(node.node_id for node in yaml_plan.nodes) == case.node_ids
    assert yaml_plan.required_capabilities == case.required_capabilities
    assert yaml_plan.terminal_result_map == case.terminal_result_map
    assert yaml_plan.artifact_policy.declared_artifact_ids == case.declared_artifact_ids
    assert (
        tuple(
            (item.terminal_result, item.artifact_ids)
            for item in yaml_plan.artifact_policy.required_by_terminal
        )
        == case.required_by_terminal
    )
    assert yaml_plan.context_policy.phase_thresholds == case.phase_thresholds
    assert yaml_plan.budgets.model_dump(mode="json") == case.budgets
    if case.name == "full":
        assert yaml_plan.prompt_policy.system_instructions.endswith(
            "second instruction.\n"
        )


def _representative_resolved(
    case: RepresentativeCompiledFixture,
    filename: str,
    source_format: Literal["yaml", "json"],
) -> Any:
    parsed = HarnessSourceParser().parse(
        SourceDocument(
            logical_path=filename,
            format=source_format,
            content=(FIXTURES / filename).read_bytes(),
        )
    )
    assert parsed.diagnostics == ()
    assert parsed.source is not None
    result = compile_semantic(
        CompileInvocation.from_request(
            make_golden_compile_request(
                source_path=filename, source_format=source_format
            ).model_copy(
                update={
                    "expected_harness_id": case.harness_id,
                    "legal_terminal_results": case.legal_terminal_results,
                }
            )
        ),
        parsed.source,
        tool_snapshot=make_golden_tool_catalog_snapshot(),
        model_profile_snapshot=make_golden_model_profile_catalog_snapshot(),
    )
    assert result.diagnostics == ()
    assert result.resolved_harness is not None
    return result.resolved_harness


def _golden_resolved(filename: str, source_format: Literal["yaml", "json"]) -> Any:
    return _representative_resolved(
        REPRESENTATIVE_COMPILED_FIXTURES[0], filename, source_format
    )


def cast_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def cast_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value
