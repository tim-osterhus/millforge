"""Canonical semantic payload and source hashing tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, NamedTuple

import pytest
from millforge import CapabilityEnvelope, CapabilityGrant, CompiledModelProfile
from millforge.compiled_plan import canonical_json_serialize
from millforge.compiler import (
    CompileInvocation,
    HarnessCompileRequest,
    HarnessSource,
    HarnessSourceParser,
    SourceDocument,
    canonical_semantic_bytes,
    canonical_semantic_payload,
    compile_semantic,
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
EXPECTED_GOLDEN_YAML_DOCUMENT_SHA256 = (
    "b5b3af7ab011b595c62642fc2acb18f1d2608ac0f1f7aa954d663c07a1f397b6"
)
EXPECTED_GOLDEN_JSON_DOCUMENT_SHA256 = (
    "9d44565e575973a35e992147d745731c2ef60e8e7f4849536b93a391bd235253"
)
EXPECTED_GOLDEN_SOURCE_SHA256 = (
    "8e2bc3139692270706ba14d02cefda6537d22a88edc25161d7a2df04714f3c72"
)


class RepresentativeFixture(NamedTuple):
    name: str
    yaml_filename: str
    json_filename: str
    harness_id: str
    legal_terminal_results: tuple[str, ...]
    yaml_document_sha256: str
    json_document_sha256: str
    source_sha256: str
    compiled_sha256: str
    semantic_byte_size: int


REPRESENTATIVE_FIXTURES: tuple[RepresentativeFixture, ...] = (
    RepresentativeFixture(
        name="full",
        yaml_filename="golden_harness.yaml",
        json_filename="golden_harness.json",
        harness_id="millforge.test.golden.compiler.v1",
        legal_terminal_results=("BLOCKED", "BUILDER_COMPLETE"),
        yaml_document_sha256=EXPECTED_GOLDEN_YAML_DOCUMENT_SHA256,
        json_document_sha256=EXPECTED_GOLDEN_JSON_DOCUMENT_SHA256,
        source_sha256=EXPECTED_GOLDEN_SOURCE_SHA256,
        compiled_sha256=(
            "4456175a8853c4814f4a5d93d2f0c4b3453d1c40fedad5c47d92218c811a3944"
        ),
        semantic_byte_size=5915,
    ),
    RepresentativeFixture(
        name="simple_success",
        yaml_filename="representative_simple_success.yaml",
        json_filename="representative_simple_success.json",
        harness_id="millforge.test.representative.simple.v1",
        legal_terminal_results=("BUILDER_COMPLETE",),
        yaml_document_sha256=(
            "414678979dcc106086a157070aabf8f40f02154a58222f131b270959819f7ded"
        ),
        json_document_sha256=(
            "02571dfdbc9b1cc2266dd9bd82901f8f7528afd645e176dc32d10fd654d5b388"
        ),
        source_sha256=(
            "cfe1c0566a00fab8294c1ba44d247b20a3e3dc30ac14021b85bf264eb02f0832"
        ),
        compiled_sha256=(
            "fc90e12d7246d91178d8ce22fa79f92416897e272fdaf4ba550df49bf97df6c2"
        ),
        semantic_byte_size=2737,
    ),
    RepresentativeFixture(
        name="blocked_artifact",
        yaml_filename="representative_blocked_artifact.yaml",
        json_filename="representative_blocked_artifact.json",
        harness_id="millforge.test.representative.blocked.v1",
        legal_terminal_results=("BLOCKED",),
        yaml_document_sha256=(
            "d3bc48422c1450e803e2187d8200ca003c62c57e55d67becac5ef76de5e856ba"
        ),
        json_document_sha256=(
            "d56932c3bd40588eb6e66aadb06954dab4825d34def449c0efbe56c2eac82086"
        ),
        source_sha256=(
            "1a39381f7fe3cbef4d1cc62b0619dbef70ef8b337dc3878da42ef6b8eb7157f7"
        ),
        compiled_sha256=(
            "3825417d4ff5848d0bcf174b07a81d4ebab1bf139f8e917dc7b722ca261efc72"
        ),
        semantic_byte_size=3828,
    ),
)


def _request() -> HarnessCompileRequest:
    return HarnessCompileRequest(
        request_id="request.canonicalization.v1",
        source_path="logical/harness.yaml",
        source_root="/tmp/source-a",
        source_format="yaml",
        output_dir="compiled",
        output_root="/tmp/output-a",
        expected_harness_id="millforge.test.canonical.v1",
        stage_kind_id="builder",
        legal_terminal_results=("BUILDER_COMPLETE",),
        capability_envelope=CapabilityEnvelope(
            grants=(
                CapabilityGrant(capability_id="artifact.write"),
                CapabilityGrant(capability_id="workspace.read"),
            )
        ),
    )


def _payload(*, ordered: bool = True) -> dict[str, Any]:
    read_node: dict[str, Any] = {
        "node_id": "read_file",
        "tool_ref": "tools.read_file@1",
        "required": True,
        "produces": ["draft", "report"] if ordered else ["report", "draft"],
    }
    write_node: dict[str, Any] = {
        "node_id": "write_report",
        "tool_ref": "tools.write_report@1",
        "prerequisites": [
            {
                "node_id": "read_file",
                "argument_matches": (
                    [
                        {"prior_argument": "beta", "current_argument": "beta"},
                        {"prior_argument": "alpha", "current_argument": "alpha"},
                    ]
                    if ordered
                    else [
                        {"prior_argument": "alpha", "current_argument": "alpha"},
                        {"prior_argument": "beta", "current_argument": "beta"},
                    ]
                ),
            }
        ],
    }
    done_node: dict[str, Any] = {
        "node_id": "done",
        "tool_ref": "tools.done@1",
        "terminal_result": "BUILDER_COMPLETE",
        "prerequisites": [{"node_id": "write_report"}],
    }
    nodes = (
        [read_node, write_node, done_node]
        if ordered
        else [done_node, write_node, read_node]
    )
    return {
        "schema_version": "1.0",
        "kind": "millforge_harness",
        "harness_id": "millforge.test.canonical.v1",
        "harness_version": 1,
        "stage_scope": {
            "stage_kind_ids": ["builder", "checker"]
            if ordered
            else ["checker", "builder"]
        },
        "model_profile_id": "profile.standard",
        "prompt": {
            "policy_id": "millforge.test.policy.v1",
            "system_instructions": "Line one.\nLine two.\n",
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


def _yaml_source() -> str:
    return """\
schema_version: "1.0"
kind: millforge_harness
harness_id: millforge.test.canonical.v1
harness_version: 1
stage_scope:
  stage_kind_ids:
    - builder
    - checker
model_profile_id: profile.standard
prompt:
  policy_id: millforge.test.policy.v1
  system_instructions: |
    Line one.
    Line two.
  include_request_context: true
budgets:
  max_iterations: 4
  max_validation_retries: 1
  max_tool_errors: 1
  max_prerequisite_violations: 1
  max_premature_terminal_attempts: 1
context:
  strategy_id: forge.tiered.v1
  budget_tokens: 12000
  keep_recent_iterations: 1
  phase_thresholds: [0.6, 0.75, 0.9]
graph:
  nodes:
    read_file:
      tool_ref: tools.read_file@1
      required: true
      produces: ["draft", "report"]
    write_report:
      tool_ref: tools.write_report@1
      prerequisites:
        - node_id: read_file
          argument_matches:
            beta: beta
            alpha: alpha
    done:
      tool_ref: tools.done@1
      terminal_result: BUILDER_COMPLETE
      prerequisites:
        - node_id: write_report
artifacts:
  declared_artifact_ids: ["report", "draft"]
  required_by_terminal:
    BUILDER_COMPLETE: ["report", "draft"]
"""


def _tool_snapshot() -> StaticToolCatalogSnapshot:
    return StaticToolCatalogSnapshot(
        entries={
            ("tools.read_file", 1): make_raw_tool_descriptor(
                tool_id="tools.read_file",
                implementation_id="impl.tools.read_file.v1",
                model_tool_name="read_file",
                input_schema={
                    "type": "object",
                    "properties": {
                        "alpha": {"type": "string"},
                        "beta": {"type": "string"},
                    },
                    "required": ["alpha", "beta"],
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
            ),
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
        }
    )


def _model_snapshot() -> StaticModelProfileCatalogSnapshot:
    return StaticModelProfileCatalogSnapshot(
        profiles={
            "profile.standard": CompiledModelProfile(profile_id="profile.standard")
        }
    )


def _resolved(source: HarnessSource) -> Any:
    result = compile_semantic(
        CompileInvocation.from_request(_request()),
        source,
        tool_snapshot=_tool_snapshot(),
        model_profile_snapshot=_model_snapshot(),
    )
    assert result.diagnostics == ()
    assert result.resolved_harness is not None
    return result.resolved_harness


def test_canonical_semantic_payload_excludes_request_parser_and_catalog_metadata() -> (
    None
):
    resolved = _resolved(HarnessSource.model_validate(_payload()))
    payload = canonical_semantic_payload(resolved)
    serialized = canonical_json_serialize(payload)

    assert payload["stage_kind_ids"] == ["builder", "checker"]
    context = cast_mapping(payload["context"])
    prompt = cast_mapping(payload["prompt"])
    assert context["phase_thresholds"] == [0.6, 0.75, 0.9]
    assert prompt["system_instructions"] == "Line one.\nLine two.\n"
    assert "invocation" not in payload
    assert "tool_snapshot" not in payload
    assert "model_profile_snapshot" not in payload
    for forbidden in (
        "request.canonicalization.v1",
        "/tmp/source-a",
        "/tmp/output-a",
        "logical/harness.yaml",
        "source_format",
        "snapshot_id",
    ):
        assert forbidden not in serialized


def test_source_sha256_hashes_canonical_semantic_bytes_and_uses_fresh_values() -> None:
    resolved = _resolved(HarnessSource.model_validate(_payload()))
    payload = canonical_semantic_payload(resolved)
    digest = source_sha256(resolved)

    assert digest == hashlib.sha256(canonical_semantic_bytes(resolved)).hexdigest()
    assert (
        digest
        == hashlib.sha256(canonical_json_serialize(payload).encode("utf-8")).hexdigest()
    )

    nodes = cast_list(payload["nodes"])
    first_node = nodes[0]
    assert isinstance(first_node, dict)
    descriptor = first_node["descriptor"]
    assert isinstance(descriptor, dict)
    input_schema = descriptor["input_schema"]
    assert isinstance(input_schema, dict)
    input_schema["mutated"] = True

    fresh = canonical_semantic_payload(resolved)
    fresh_nodes = cast_list(fresh["nodes"])
    fresh_node = fresh_nodes[0]
    assert isinstance(fresh_node, dict)
    fresh_descriptor = fresh_node["descriptor"]
    assert isinstance(fresh_descriptor, dict)
    assert "mutated" not in cast_mapping(fresh_descriptor["input_schema"])


def test_canonical_semantic_payload_sorts_incidental_source_and_catalog_order() -> None:
    left = _resolved(HarnessSource.model_validate(_payload(ordered=True)))
    right = _resolved(HarnessSource.model_validate(_payload(ordered=False)))

    assert canonical_semantic_payload(left) == canonical_semantic_payload(right)
    assert source_sha256(left) == source_sha256(right)


@pytest.mark.parametrize("case", REPRESENTATIVE_FIXTURES, ids=lambda case: case.name)
def test_representative_yaml_and_json_pin_canonical_semantic_bytes_and_hashes(
    case: RepresentativeFixture,
) -> None:
    yaml_resolved, yaml_document_sha = _representative_resolved(
        case, case.yaml_filename, "yaml"
    )
    json_resolved, json_document_sha = _representative_resolved(
        case, case.json_filename, "json"
    )
    yaml_semantic = canonical_semantic_bytes(yaml_resolved)
    json_semantic = canonical_semantic_bytes(json_resolved)

    assert yaml_document_sha == case.yaml_document_sha256
    assert json_document_sha == case.json_document_sha256
    assert yaml_document_sha != json_document_sha
    assert yaml_semantic == json_semantic
    assert len(yaml_semantic) == case.semantic_byte_size
    assert hashlib.sha256(yaml_semantic).hexdigest() == case.source_sha256
    assert source_sha256(yaml_resolved) == case.source_sha256
    assert source_sha256(json_resolved) == case.source_sha256
    if case.name == "full":
        assert (
            yaml_semantic == (FIXTURES / "golden_canonical_semantic.json").read_bytes()
        )


def _representative_resolved(
    case: RepresentativeFixture, filename: str, source_format: Literal["yaml", "json"]
) -> tuple[Any, str]:
    document = SourceDocument(
        logical_path=filename,
        format=source_format,
        content=(FIXTURES / filename).read_bytes(),
    )
    parsed = HarnessSourceParser().parse(document)
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
    return result.resolved_harness, parsed.source_document_sha256


def _golden_resolved(
    filename: str, source_format: Literal["yaml", "json"]
) -> tuple[Any, str]:
    return _representative_resolved(REPRESENTATIVE_FIXTURES[0], filename, source_format)


def test_formatting_equivalent_yaml_and_json_have_identical_semantic_hashes() -> None:
    parser = HarnessSourceParser()
    yaml_result = parser.parse(
        SourceDocument(
            logical_path="source-a.yaml",
            format="yaml",
            content=_yaml_source().encode(),
        )
    )
    json_result = parser.parse(
        SourceDocument(
            logical_path="other/source.json",
            format="json",
            content=json.dumps(_payload(), indent=2).encode(),
        )
    )

    assert yaml_result.diagnostics == ()
    assert json_result.diagnostics == ()
    assert yaml_result.source is not None
    assert json_result.source is not None
    assert yaml_result.source_document_sha256 != json_result.source_document_sha256
    assert source_sha256(_resolved(yaml_result.source)) == source_sha256(
        _resolved(json_result.source)
    )


def test_semantic_changes_alter_source_sha256() -> None:
    original = _payload()
    changed = _payload()
    prompt = changed["prompt"]
    assert isinstance(prompt, dict)
    prompt["system_instructions"] = "Line two.\nLine one.\n"

    assert source_sha256(_resolved(HarnessSource.model_validate(original))) != (
        source_sha256(_resolved(HarnessSource.model_validate(changed)))
    )


@pytest.mark.parametrize("case", REPRESENTATIVE_FIXTURES, ids=lambda case: case.name)
def test_representative_semantic_changes_alter_source_sha256(
    case: RepresentativeFixture,
) -> None:
    payload = json.loads((FIXTURES / case.json_filename).read_text(encoding="utf-8"))
    prompt = payload["prompt"]
    assert isinstance(prompt, dict)
    prompt["system_instructions"] = f"{prompt['system_instructions']}Changed.\n"
    changed = HarnessSource.model_validate(payload)
    changed_result = compile_semantic(
        CompileInvocation.from_request(
            make_golden_compile_request(
                source_path=case.json_filename,
                source_format="json",
            ).model_copy(
                update={
                    "expected_harness_id": case.harness_id,
                    "legal_terminal_results": case.legal_terminal_results,
                }
            )
        ),
        changed,
        tool_snapshot=make_golden_tool_catalog_snapshot(),
        model_profile_snapshot=make_golden_model_profile_catalog_snapshot(),
    )

    assert changed_result.diagnostics == ()
    assert changed_result.resolved_harness is not None
    changed_plan = lower_resolved_harness(changed_result.resolved_harness)
    assert changed_plan.source_sha256 != case.source_sha256
    assert changed_plan.compiled_sha256 != case.compiled_sha256


def cast_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def cast_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value
