"""Tests for parser boundary records."""

from __future__ import annotations

import hashlib
import json
import time

import pytest
from typing import cast

from pydantic import ValidationError

from millforge.compiler import HarnessSourceParser, ParsedHarnessSource, SourceDocument

SHA_A = "a" * 64


def _source_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "kind": "millforge_harness",
        "harness_id": "millforge.test.builder.compiler.v1",
        "harness_version": 1,
        "stage_scope": {"stage_kind_ids": ["builder"]},
        "model_profile_id": "fake.builder.v1",
        "prompt": {
            "policy_id": "millforge.test.builder.policy.v1",
            "system_instructions": (
                "Use the admitted tools to complete the request.\n"
                "Do not claim actions that were not observed.\n"
            ),
            "include_request_context": True,
        },
        "budgets": {
            "max_iterations": 12,
            "max_validation_retries": 2,
            "max_tool_errors": 2,
            "max_prerequisite_violations": 2,
            "max_premature_terminal_attempts": 2,
        },
        "context": {
            "strategy_id": "forge.tiered.v1",
            "budget_tokens": 12000,
            "keep_recent_iterations": 2,
            "phase_thresholds": [0.60, 0.75, 0.90],
        },
        "graph": {
            "nodes": {
                "inspect_request": {
                    "tool_ref": "builtin.request.inspect@1",
                    "required": True,
                },
                "read_file": {"tool_ref": "builtin.workspace.read_file@1"},
                "apply_patch": {
                    "tool_ref": "builtin.workspace.apply_patch@1",
                    "prerequisites": [
                        {
                            "node_id": "read_file",
                            "argument_matches": {"path": "path"},
                        }
                    ],
                },
                "write_patch_summary": {
                    "tool_ref": "builtin.artifact.write_patch_summary@1",
                    "produces": ["patch_summary"],
                    "prerequisites": [{"node_id": "apply_patch"}],
                },
                "submit_patch": {
                    "tool_ref": "builtin.terminal.submit@1",
                    "terminal_result": "BUILDER_COMPLETE",
                    "prerequisites": [{"node_id": "write_patch_summary"}],
                },
                "block_builder": {
                    "tool_ref": "builtin.terminal.escalate@1",
                    "terminal_result": "BUILDER_BLOCKED",
                },
            }
        },
        "artifacts": {
            "declared_artifact_ids": ["patch_summary"],
            "required_by_terminal": {"BUILDER_COMPLETE": ["patch_summary"]},
        },
    }


def _source_yaml() -> str:
    return """\
schema_version: "1.0"
kind: millforge_harness
harness_id: millforge.test.builder.compiler.v1
harness_version: 1
stage_scope:
  stage_kind_ids: ["builder"]
model_profile_id: fake.builder.v1
prompt:
  policy_id: millforge.test.builder.policy.v1
  system_instructions: |
    Use the admitted tools to complete the request.
    Do not claim actions that were not observed.
  include_request_context: true
budgets:
  max_iterations: 12
  max_validation_retries: 2
  max_tool_errors: 2
  max_prerequisite_violations: 2
  max_premature_terminal_attempts: 2
context:
  strategy_id: forge.tiered.v1
  budget_tokens: 12000
  keep_recent_iterations: 2
  phase_thresholds: [0.60, 0.75, 0.90]
graph:
  nodes:
    inspect_request:
      tool_ref: builtin.request.inspect@1
      required: true

    read_file:
      tool_ref: builtin.workspace.read_file@1

    apply_patch:
      tool_ref: builtin.workspace.apply_patch@1
      prerequisites:
        - node_id: read_file
          argument_matches:
            path: path

    write_patch_summary:
      tool_ref: builtin.artifact.write_patch_summary@1
      produces:
        - patch_summary
      prerequisites:
        - node_id: apply_patch

    submit_patch:
      tool_ref: builtin.terminal.submit@1
      terminal_result: BUILDER_COMPLETE
      prerequisites:
        - node_id: write_patch_summary

    block_builder:
      tool_ref: builtin.terminal.escalate@1
      terminal_result: BUILDER_BLOCKED
artifacts:
  declared_artifact_ids: ["patch_summary"]
  required_by_terminal:
    BUILDER_COMPLETE: ["patch_summary"]
"""


def test_source_document_excludes_content_from_repr_and_is_closed() -> None:
    document = SourceDocument(
        logical_path="harness.yaml",
        format="yaml",
        content=b"schema_version: '1.0'\n",
    )

    assert "schema_version" not in repr(document)
    with pytest.raises(ValidationError):
        SourceDocument(
            logical_path="harness.yaml",
            format=cast(str, 42),
            content=b"",
        )


def test_public_parser_reports_unsupported_format_as_mf_s005_without_source_leak() -> (
    None
):
    content = b'api_key = "sk-test-secret-secret-secret"\n'

    parsed = HarnessSourceParser().parse(
        SourceDocument(logical_path="harness.toml", format="toml", content=content)
    )

    assert parsed.source is None
    assert parsed.source_document_sha256 == hashlib.sha256(content).hexdigest()
    diagnostic = parsed.diagnostics[0]
    assert diagnostic.code == "MF-S005"
    assert diagnostic.phase.value == "parse"
    assert diagnostic.message == "Unsupported source format."
    assert diagnostic.source_reference is not None
    assert diagnostic.source_reference.logical_path == "harness.toml"
    serialized = diagnostic.model_dump_json()
    rendered = str(diagnostic)
    assert "api_key" not in serialized
    assert "sk-test-secret-secret-secret" not in serialized
    assert "api_key" not in rendered
    assert "sk-test-secret-secret-secret" not in rendered


def test_parsed_harness_source_rejects_bad_hash_and_snapshots_locations() -> None:
    parsed = ParsedHarnessSource(
        source=None,
        source_document_sha256=SHA_A,
        diagnostics=(),
        location_index=(),
    )
    assert parsed.diagnostics == ()
    assert parsed.location_index == ()

    with pytest.raises(ValidationError):
        ParsedHarnessSource(source=None, source_document_sha256="A" * 64)


def test_harness_source_parser_accepts_equivalent_yaml_and_json() -> None:
    parser = HarnessSourceParser()
    yaml_result = parser.parse(
        SourceDocument(
            logical_path="harness.yaml",
            format="yaml",
            content=_source_yaml().encode(),
        )
    )
    json_bytes = json.dumps(_source_payload(), separators=(",", ":")).encode()
    json_result = parser.parse(
        SourceDocument(logical_path="harness.json", format="json", content=json_bytes)
    )

    assert yaml_result.diagnostics == ()
    assert json_result.diagnostics == ()
    assert yaml_result.source is not None
    assert json_result.source is not None
    assert yaml_result.source == json_result.source
    assert (
        yaml_result.source.prompt.system_instructions
        == "Use the admitted tools to complete the request.\n"
        "Do not claim actions that were not observed.\n"
    )
    assert (
        yaml_result.source.graph.nodes[2]
        .prerequisites[0]
        .argument_matches[0]
        .prior_argument
        == "path"
    )
    assert yaml_result.location_index
    assert json_result.location_index
    yaml_paths = {reference.field_path for reference in yaml_result.location_index}
    json_paths = {reference.field_path for reference in json_result.location_index}
    assert "/graph/nodes/2/prerequisites/0/node_id" in yaml_paths
    assert "/graph/nodes/2/prerequisites/0/argument_matches/0" in yaml_paths
    assert "/graph/nodes/2/prerequisites/0/node_id" in json_paths
    assert "/graph/nodes/2/prerequisites/0/argument_matches/0" in json_paths


def test_json_parser_accepts_leading_whitespace() -> None:
    json_bytes = ("\n  \t" + json.dumps(_source_payload())).encode()
    parsed = HarnessSourceParser().parse(
        SourceDocument(logical_path="harness.json", format="json", content=json_bytes)
    )

    assert parsed.diagnostics == ()
    assert parsed.source is not None


def test_source_document_hash_removes_bom_and_normalizes_newlines() -> None:
    parser = HarnessSourceParser()
    canonical = _source_yaml().encode()
    variant = b"\xef\xbb\xbf" + _source_yaml().replace("\n", "\r\n").encode()
    parsed = parser.parse(
        SourceDocument(logical_path="harness.yaml", format="yaml", content=variant)
    )

    assert parsed.diagnostics == ()
    assert parsed.source_document_sha256 == hashlib.sha256(canonical).hexdigest()


def test_source_document_size_limit_is_reported_by_parser() -> None:
    parsed = HarnessSourceParser().parse(
        SourceDocument(
            logical_path="harness.yaml",
            format="yaml",
            content=b"a" * 1_048_577,
        )
    )

    assert parsed.source is None
    assert parsed.diagnostics[0].code == "MF-S003"


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b'{"schema_version":"1.0","schema_version":"1.0"}', "MF-S006"),
        (b'{"schema_version":"1.0","schema\\u005fversion":"1.0"}', "MF-S006"),
        (b'{"schema_version":"1.0"} trailing', "MF-S011"),
        (b'["not", "object"]', "MF-S012"),
        (b'{"schema_version": NaN}', "MF-S011"),
        (b'{"schema_version":"\\u0000"}', "MF-S011"),
        (b'{"schema_version":"\\ud800"}', "MF-S011"),
        (b'{"schema_version":"' + b"a" * 65_537 + b'"}', "MF-S010"),
        (b'{"schema_version":' + b"1" * 129 + b"}", "MF-S010"),
        (b'{"schema_version":1.' + b"1" * 127 + b"}", "MF-S010"),
    ],
)
def test_json_parser_rejects_unsafe_or_ambiguous_input(
    content: bytes, code: str
) -> None:
    parsed = HarnessSourceParser().parse(
        SourceDocument(logical_path="harness.json", format="json", content=content)
    )

    assert parsed.source is None
    assert parsed.diagnostics[0].code == code


def test_parser_adversarial_matrix_is_bounded_and_does_not_echo_payloads() -> None:
    def _json_bytes(payload: object) -> bytes:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()

    def _deep_yaml() -> bytes:
        return (
            "".join(f"{'  ' * index}a{index}:\n" for index in range(33))
            + "  " * 33
            + "leaf: value\n"
        ).encode()

    large_scalar = "SENSITIVE_SCALAR_" + ("x" * 65_537)
    huge_integer = "9" * 129
    confusable_harness_id = "millforge.test.bu\u0456lder.v1"
    confusable_payload = _source_payload()
    confusable_payload["harness_id"] = confusable_harness_id
    confusable_payload_bytes = _json_bytes(confusable_payload)
    bool_budget_payload = _source_payload()
    budgets = bool_budget_payload["budgets"]
    assert isinstance(budgets, dict)
    budgets["max_iterations"] = True
    bool_budget_payload_bytes = _json_bytes(bool_budget_payload)
    wide_json_payload = {f"key_{index}": index for index in range(10_001)}
    wide_json_payload_bytes = _json_bytes(wide_json_payload)
    wide_yaml_sequence = ("items:\n" + "  - x\n" * 10_001).encode()
    deep_yaml = _deep_yaml()
    cases = [
        ("json-empty", "json", b"", "MF-S011", "parse", None),
        ("json-whitespace", "json", b" \n\t ", "MF-S011", "parse", None),
        ("json-invalid-utf8", "json", b"\xff", "MF-S004", "parse", None),
        (
            "json-top-level-scalar",
            "json",
            b'"not_object"',
            "MF-S012",
            "parse",
            "not_object",
        ),
        (
            "json-top-level-array",
            "json",
            b'["not","object"]',
            "MF-S012",
            "parse",
            None,
        ),
        (
            "json-trailing-content",
            "json",
            b'{"schema_version":"1.0"} []',
            "MF-S011",
            "parse",
            None,
        ),
        (
            "json-duplicate-keys",
            "json",
            b'{"schema_version":"1.0","schema_version":"1.0"}',
            "MF-S006",
            "parse",
            None,
        ),
        (
            "json-nan",
            "json",
            b'{"schema_version":NaN}',
            "MF-S011",
            "parse",
            "NaN",
        ),
        (
            "json-infinity",
            "json",
            b'{"schema_version":Infinity}',
            "MF-S011",
            "parse",
            "Infinity",
        ),
        (
            "json-negative-infinity",
            "json",
            b'{"schema_version":-Infinity}',
            "MF-S011",
            "parse",
            "-Infinity",
        ),
        (
            "json-embedded-nul",
            "json",
            b'{"schema_version":"\\u0000"}',
            "MF-S011",
            "parse",
            "\\u0000",
        ),
        (
            "json-wide-limit",
            "json",
            wide_json_payload_bytes,
            "MF-S010",
            "parse",
            "key_10000",
        ),
        (
            "json-large-scalar",
            "json",
            f'{{"secret":"{large_scalar}"}}'.encode(),
            "MF-S010",
            "parse",
            large_scalar,
        ),
        (
            "json-extreme-integer",
            "json",
            f'{{"value":{huge_integer}}}'.encode(),
            "MF-S010",
            "parse",
            huge_integer,
        ),
        (
            "json-confusable-harness-id",
            "json",
            confusable_payload_bytes,
            "MF-S022",
            "schema",
            confusable_harness_id,
        ),
        (
            "json-bool-int-field",
            "json",
            bool_budget_payload_bytes,
            "MF-S024",
            "schema",
            "true",
        ),
        ("yaml-empty", "yaml", b"", "MF-S012", "parse", None),
        ("yaml-whitespace", "yaml", b" \n  \n", "MF-S012", "parse", None),
        (
            "yaml-multiple-documents",
            "yaml",
            b"---\na: b\n---\nc: d\n",
            "MF-S009",
            "parse",
            None,
        ),
        (
            "yaml-duplicate-keys",
            "yaml",
            b"schema_version: 1\nschema_version: 2\n",
            "MF-S006",
            "parse",
            None,
        ),
        (
            "yaml-anchor-alias",
            "yaml",
            b"schema_version: &v 1\nother: *v\n",
            "MF-S007",
            "parse",
            "&v",
        ),
        (
            "yaml-unsafe-tag",
            "yaml",
            b"schema_version: !custom value\n",
            "MF-S008",
            "parse",
            "!custom",
        ),
        (
            "yaml-timestamp",
            "yaml",
            b"schema_version: 2026-06-14\n",
            "MF-S008",
            "parse",
            "2026-06-14",
        ),
        (
            "yaml-binary",
            "yaml",
            b"binary: !!binary SGVsbG8=\n",
            "MF-S008",
            "parse",
            "!!binary",
        ),
        (
            "yaml-set",
            "yaml",
            b"set: !!set {a: null}\n",
            "MF-S008",
            "parse",
            "!!set",
        ),
        (
            "yaml-non-string-key",
            "yaml",
            b"? [a, b]: value\n",
            "MF-S008",
            "parse",
            "[a, b]",
        ),
        (
            "yaml-wide-sequence-limit",
            "yaml",
            wide_yaml_sequence,
            "MF-S010",
            "parse",
            None,
        ),
        (
            "yaml-merge",
            "yaml",
            b"base: &base {a: b}\n<<: *base\n",
            "MF-S007",
            "parse",
            None,
        ),
        ("yaml-deep-structure", "yaml", deep_yaml, "MF-S010", "parse", None),
    ]

    parser = HarnessSourceParser()
    for (
        name,
        source_format,
        content,
        expected_code,
        expected_phase,
        rejected_text,
    ) in cases:
        started = time.perf_counter()
        parsed = parser.parse(
            SourceDocument(
                logical_path=f"{name}.{'json' if source_format == 'json' else 'yaml'}",
                format=source_format,
                content=content,
            )
        )
        elapsed = time.perf_counter() - started

        assert elapsed < 5.0, name
        assert parsed.source is None, name
        diagnostic = parsed.diagnostics[0]
        assert diagnostic.code == expected_code, name
        assert diagnostic.phase.value == expected_phase, name
        rendered = diagnostic.model_dump_json() + repr(diagnostic) + str(diagnostic)
        decoded = content.decode("utf-8", errors="ignore")
        if decoded:
            assert decoded not in rendered
        if rejected_text is not None:
            assert rejected_text not in rendered


def test_yaml_duplicate_keys_compare_decoded_scalars() -> None:
    parsed = HarnessSourceParser().parse(
        SourceDocument(
            logical_path="harness.yaml",
            format="yaml",
            content=b'schema_version: 1\n"schema_version": 2\n',
        )
    )

    assert parsed.source is None
    assert parsed.diagnostics[0].code == "MF-S006"


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_json_parser_nonfinite_number_errors_do_not_echo_source_tokens(
    token: str,
) -> None:
    parsed = HarnessSourceParser().parse(
        SourceDocument(
            logical_path="harness.json",
            format="json",
            content=f'{{"schema_version": {token}}}'.encode(),
        )
    )

    diagnostic = parsed.diagnostics[0]
    assert diagnostic.code == "MF-S011"
    assert diagnostic.message == "Non-finite number is not allowed."
    assert token not in diagnostic.message
    assert token not in repr(diagnostic)
    assert token not in str(diagnostic)
    assert token not in diagnostic.model_dump_json()


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("schema_version: 1\nschema_version: 2\n", "MF-S006"),
        ("schema_version: &v 1\nother: *v\n", "MF-S007"),
        ("schema_version: !custom value\n", "MF-S008"),
        ("schema_version: 2026-06-14\n", "MF-S008"),
        ("binary: !!binary SGVsbG8=\n", "MF-S008"),
        ("set: !!set {a: null}\n", "MF-S008"),
        ("base: &base {a: b}\n<<: *base\n", "MF-S007"),
        ("? [a, b]: value\n", "MF-S008"),
        ("---\nschema_version: 1\n---\nother: 2\n", "MF-S009"),
        ("schema_version: .nan\n", "MF-S011"),
    ],
)
def test_yaml_parser_rejects_non_json_yaml_constructs(text: str, code: str) -> None:
    parsed = HarnessSourceParser().parse(
        SourceDocument(
            logical_path="harness.yaml",
            format="yaml",
            content=text.encode(),
        )
    )

    assert parsed.source is None
    assert parsed.diagnostics[0].code == code


def test_parser_limits_and_schema_unknown_fields_are_reported() -> None:
    too_deep = "".join(f"{'  ' * index}a{index}:\n" for index in range(33))
    too_deep += "  " * 33 + "leaf: value\n"
    parsed = HarnessSourceParser().parse(
        SourceDocument(
            logical_path="harness.yaml",
            format="yaml",
            content=too_deep.encode(),
        )
    )
    assert parsed.diagnostics[0].code == "MF-S010"

    payload = _source_payload()
    prompt = payload["prompt"]
    assert isinstance(prompt, dict)
    prompt["unknown"] = "field"
    parsed = HarnessSourceParser().parse(
        SourceDocument(
            logical_path="harness.json",
            format="json",
            content=json.dumps(payload).encode(),
        )
    )
    assert parsed.source is None
    assert parsed.diagnostics[0].code == "MF-S021"
    assert parsed.diagnostics[0].source_reference is not None
    assert parsed.diagnostics[0].source_reference.field_path.startswith("/prompt")
    assert parsed.diagnostics[0].source_reference.location is not None

    many_entries = {f"key_{index}": index for index in range(10_001)}
    parsed = HarnessSourceParser().parse(
        SourceDocument(
            logical_path="harness.json",
            format="json",
            content=json.dumps(many_entries, separators=(",", ":")).encode(),
        )
    )
    assert parsed.source is None
    assert parsed.diagnostics[0].code == "MF-S010"


def test_location_index_uses_one_based_unicode_code_point_columns() -> None:
    payload = {"\U0001f680": 0, **_source_payload()}
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    parsed = HarnessSourceParser().parse(
        SourceDocument(logical_path="harness.json", format="json", content=content)
    )

    harness_id = next(
        reference
        for reference in parsed.location_index
        if reference.field_path == "/schema_version"
    )
    assert harness_id.location is not None
    assert harness_id.location.line == 1
    assert harness_id.location.column == 8


def test_yaml_sequence_item_schema_errors_receive_precise_locations() -> None:
    parser = HarnessSourceParser()
    bad_node_id = _source_yaml().replace("node_id: read_file", "node_id: READ_FILE")
    parsed = parser.parse(
        SourceDocument(
            logical_path="harness.yaml",
            format="yaml",
            content=bad_node_id.encode(),
        )
    )

    diagnostic = parsed.diagnostics[0]
    assert diagnostic.code == "MF-S022"
    assert diagnostic.source_reference is not None
    assert (
        diagnostic.source_reference.field_path
        == "/graph/nodes/2/prerequisites/0/node_id"
    )
    assert diagnostic.source_reference.location is not None

    bad_argument = _source_yaml().replace("path: path", "path: 1path")
    parsed = parser.parse(
        SourceDocument(
            logical_path="harness.yaml",
            format="yaml",
            content=bad_argument.encode(),
        )
    )

    diagnostic = parsed.diagnostics[0]
    assert diagnostic.code == "MF-S022"
    assert diagnostic.source_reference is not None
    assert diagnostic.source_reference.field_path.startswith(
        "/graph/nodes/2/prerequisites/0/argument_matches"
    )
    assert diagnostic.source_reference.location is not None


@pytest.mark.parametrize(
    ("mutator", "expected_code", "expected_path"),
    [
        (
            lambda payload: payload["prompt"].__setitem__(
                "api_key", "sk-test-secret-secret-secret"
            ),
            "MF-S021",
            "/prompt/api_key",
        ),
        (
            lambda payload: payload.__setitem__("harness_id", "Millforge"),
            "MF-S022",
            "/harness_id",
        ),
        (
            lambda payload: payload["graph"]["nodes"]["read_file"].__setitem__(
                "tool_ref", "builtin.workspace.read_file"
            ),
            "MF-S023",
            "/graph/nodes/1/tool_ref",
        ),
        (
            lambda payload: payload["budgets"].__setitem__("max_iterations", 0),
            "MF-S024",
            "/budgets/max_iterations",
        ),
        (
            lambda payload: payload["context"].__setitem__(
                "phase_thresholds", [0.8, 0.7, 0.9]
            ),
            "MF-S025",
            "/context/phase_thresholds",
        ),
    ],
)
def test_source_schema_validation_classes_emit_exact_codes(
    mutator: object,
    expected_code: str,
    expected_path: str,
) -> None:
    payload = _source_payload()
    assert callable(mutator)
    mutator(payload)

    parsed = HarnessSourceParser().parse(
        SourceDocument(
            logical_path="harness.json",
            format="json",
            content=json.dumps(payload, separators=(",", ":")).encode(),
        )
    )

    assert parsed.source is None
    diagnostic = parsed.diagnostics[0]
    assert diagnostic.code == expected_code
    assert diagnostic.phase.value == "schema"
    assert diagnostic.source_reference is not None
    assert diagnostic.source_reference.field_path == expected_path
    assert diagnostic.source_reference.location is not None
    assert diagnostic.fields[0].key == "field_path"
    assert diagnostic.fields[0].value == expected_path
    serialized = diagnostic.model_dump_json()
    assert "sk-test-secret-secret-secret" not in serialized
    assert "input" not in serialized
    assert "url" not in serialized


def test_source_schema_validation_diagnostics_are_sorted_and_bounded() -> None:
    payload = _source_payload()
    payload["z_unknown"] = "z"
    payload["a_unknown"] = "a"
    prompt = payload["prompt"]
    assert isinstance(prompt, dict)
    for index in range(140):
        prompt[f"unknown_{index:03d}"] = "x" * 2048

    parsed = HarnessSourceParser().parse(
        SourceDocument(
            logical_path="harness.json",
            format="json",
            content=json.dumps(payload, separators=(",", ":")).encode(),
        )
    )

    assert parsed.source is None
    assert parsed.diagnostics[0].source_reference is not None
    assert parsed.diagnostics[0].source_reference.field_path == "/prompt/unknown_000"
    assert parsed.diagnostics[-1].code == "MF-D001"
    serialized = json.dumps(
        [diagnostic.model_dump(mode="json") for diagnostic in parsed.diagnostics],
        sort_keys=True,
        separators=(",", ":"),
    )
    assert len(serialized.encode("utf-8")) <= 256 * 1024


def test_residual_source_schema_failures_remain_generic() -> None:
    payload = _source_payload()
    payload["schema_version"] = "2.0"

    parsed = HarnessSourceParser().parse(
        SourceDocument(
            logical_path="harness.json",
            format="json",
            content=json.dumps(payload, separators=(",", ":")).encode(),
        )
    )

    assert parsed.source is None
    diagnostic = parsed.diagnostics[0]
    assert diagnostic.code == "MF-S020"
    assert diagnostic.source_reference is not None
    assert diagnostic.source_reference.field_path == "/schema_version"
