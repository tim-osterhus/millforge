"""Tests for compiler-owned JSON Schema subset validation."""

from __future__ import annotations

import builtins
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import ValidationError

from millforge import IdempotencyClass, SideEffectClass
from millforge.compiler import (
    RawToolDescriptor,
    SchemaSubsetError,
    ToolCatalogEntry,
    normalize_json_schema,
    normalized_schema_bytes,
    property_schema_compatibility_bytes,
    validate_json_schema_subset,
)

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "schema_validation_golden.json"
SHA_A = "a" * 64


def make_raw_tool_descriptor(
    *,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "tool_id": "tools.echo",
        "tool_version": 1,
        "implementation_id": "impl.tools.echo.v1",
        "descriptor_sha256": SHA_A,
        "model_tool_name": "echo",
        "description": "Echo test input.",
        "input_schema": input_schema
        or {
            "type": "object",
            "additionalProperties": False,
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        "output_schema": output_schema
        or {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
        "side_effect_class": SideEffectClass.READ_ONLY,
        "idempotency": IdempotencyClass.IDEMPOTENT,
        "required_capabilities": ("workspace.read",),
        "produced_artifact_ids": ("echo_output",),
    }


def load_golden_vectors() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_golden_accepted_schema_vectors_are_normalized_and_byte_stable() -> None:
    for vector in load_golden_vectors()["accepted"]:
        assert normalize_json_schema(vector["schema"]) == vector["normalized"]
        assert normalized_schema_bytes(vector["schema"]) == vector["bytes"].encode()
        assert property_schema_compatibility_bytes(vector["schema"]) == (
            vector["bytes"].encode()
        )


def test_golden_rejected_schema_vectors_fail_closed() -> None:
    for vector in load_golden_vectors()["rejected"]:
        with pytest.raises(SchemaSubsetError, match=vector["message"]):
            normalize_json_schema(vector["schema"])


@pytest.mark.parametrize(
    ("keyword", "schema"),
    [
        ("maxLength", {"type": "string", "maxLength": 8}),
        ("maxItems", {"type": "array", "items": {"type": "string"}, "maxItems": 2}),
        ("minimum", {"type": "number", "minimum": 1}),
        ("maximum", {"type": "integer", "maximum": 10}),
    ],
)
def test_bound_keywords_are_rejected_instead_of_unenforced(
    keyword: str, schema: dict[str, Any]
) -> None:
    with pytest.raises(SchemaSubsetError, match=rf"/schema/{keyword}.*runtime"):
        normalize_json_schema(schema)


def test_validation_returns_deep_frozen_normalized_schema() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "description": "ignored",
        "additionalProperties": False,
        "properties": {
            "message": {"type": "string", "default": "ignored"},
            "count": {"type": "integer"},
        },
        "required": ["message"],
    }

    frozen = validate_json_schema_subset(schema)
    schema["properties"]["message"]["type"] = "integer"

    assert isinstance(frozen, MappingProxyType)
    assert frozen["properties"]["message"] == {"type": "string"}
    assert "description" not in frozen
    with pytest.raises(TypeError):
        frozen["properties"]["message"]["type"] = "integer"


def test_semantically_equivalent_property_schemas_have_equal_bytes() -> None:
    first = {
        "description": "A",
        "type": "string",
        "enum": ["final", "draft"],
        "default": "draft",
    }
    second = {
        "type": "string",
        "enum": ["final", "draft"],
        "description": "B",
    }

    assert property_schema_compatibility_bytes(first) == (
        property_schema_compatibility_bytes(second)
    )
    assert property_schema_compatibility_bytes(first) == (
        b'{"enum":["final","draft"],"type":"string"}'
    )


def test_enum_declared_order_participates_in_property_compatibility_bytes() -> None:
    first = {"type": "string", "enum": ["final", "draft"]}
    second = {"type": "string", "enum": ["draft", "final"]}

    assert property_schema_compatibility_bytes(first) != (
        property_schema_compatibility_bytes(second)
    )


def test_property_compatibility_bytes_exclude_output_schema_metadata() -> None:
    descriptor_a = make_raw_tool_descriptor(
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"result": {"type": "string", "description": "ignored"}},
            "required": ["result"],
        }
    )
    descriptor_b = make_raw_tool_descriptor(
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"other": {"type": "integer"}},
            "required": [],
        }
    )
    entry_a = ToolCatalogEntry.admit(
        descriptor_a,
        expected_tool_id="tools.echo",
        expected_tool_version=1,
    )
    entry_b = ToolCatalogEntry.admit(
        descriptor_b,
        expected_tool_id="tools.echo",
        expected_tool_version=1,
    )

    prop_a = entry_a.input_schema["properties"]["message"]
    prop_b = entry_b.input_schema["properties"]["message"]
    assert property_schema_compatibility_bytes(prop_a) == (
        property_schema_compatibility_bytes(prop_b)
    )
    assert entry_a.output_schema != entry_b.output_schema
    assert "description" not in entry_a.output_schema["properties"]["result"]


def test_object_property_schemas_have_equal_compatibility_bytes() -> None:
    vector = next(
        item
        for item in load_golden_vectors()["accepted"]
        if item["name"] == "object_nested_arrays"
    )
    equivalent = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tags": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "metadata": {
                "type": "object",
                "description": "Equivalent nested annotation.",
                "additionalProperties": False,
                "properties": {
                    "source": {
                        "type": "string",
                        "default": "manual",
                    }
                },
                "required": ["source"],
            },
            "name": {
                "type": "string",
                "description": "Equivalent field annotation.",
            },
        },
        "required": ["metadata", "name"],
    }

    expected = vector["bytes"].encode()
    assert property_schema_compatibility_bytes(vector["schema"]) == expected
    assert property_schema_compatibility_bytes(equivalent) == expected
    assert property_schema_compatibility_bytes(vector["schema"]) == (
        property_schema_compatibility_bytes(equivalent)
    )


def test_catalog_descriptor_admission_uses_schema_subset_contract() -> None:
    with pytest.raises(ValidationError, match="minLength"):
        RawToolDescriptor.model_validate(
            make_raw_tool_descriptor(
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"message": {"type": "string", "minLength": 3}},
                    "required": ["message"],
                }
            )
        )

    with pytest.raises(ValidationError, match="additionalProperties"):
        RawToolDescriptor.model_validate(
            make_raw_tool_descriptor(
                output_schema={
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {},
                    "required": [],
                }
            )
        )


def test_scalar_identity_rejects_numeric_duplicates_but_keeps_booleans_distinct() -> (
    None
):
    assert normalize_json_schema({"type": "number", "enum": [1, 2.0]}) == {
        "type": "number",
        "enum": [1, 2.0],
    }
    with pytest.raises(SchemaSubsetError, match="duplicate"):
        normalize_json_schema({"type": "number", "enum": [1, 1.0]})
    with pytest.raises(SchemaSubsetError, match="enum value"):
        normalize_json_schema({"type": "number", "enum": [True, 1]})


def test_scalar_const_may_replace_type_and_accept_null() -> None:
    assert normalize_json_schema({"const": None, "description": "ignored"}) == {
        "const": None
    }
    with pytest.raises(SchemaSubsetError, match="non-annotation"):
        normalize_json_schema({"type": "integer", "const": 1})
    assert property_schema_compatibility_bytes({"const": "ready"}) == (
        b'{"const":"ready"}'
    )
    with pytest.raises(SchemaSubsetError, match="non-annotation"):
        normalize_json_schema({"const": "ready", "enum": ["ready"]})


def test_recursive_array_schema_is_accepted_without_forge_imports() -> None:
    schema = {
        "type": "array",
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    }

    assert normalize_json_schema(schema)["items"]["items"]["type"] == "object"


def test_schema_validation_has_no_runtime_or_external_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden_imports = (
        "millforge._forge",
        "millforge.model_backend",
        "openai",
        "requests",
        "httpx",
        "urllib",
    )
    original_import = builtins.__import__

    def rejecting_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith(forbidden_imports):
            raise AssertionError(f"forbidden import: {name}")
        return original_import(name, *args, **kwargs)

    def forbidden_call(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("schema validation performed external side effect")

    monkeypatch.setattr(builtins, "__import__", rejecting_import)
    monkeypatch.setattr(os, "scandir", forbidden_call)
    monkeypatch.setattr(os, "listdir", forbidden_call)
    monkeypatch.setattr(os, "walk", forbidden_call)
    monkeypatch.setattr(Path, "write_text", forbidden_call)
    monkeypatch.setattr(Path, "mkdir", forbidden_call)

    assert normalize_json_schema({"const": None}) == {"const": None}
