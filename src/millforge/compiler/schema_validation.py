"""Compiler-owned JSON Schema subset validation and normalization."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

SCHEMA_ANNOTATION_KEYWORDS = frozenset({"default", "description"})
SCHEMA_STRUCTURAL_KEYWORDS = frozenset(
    {"additionalProperties", "const", "enum", "items", "properties", "required", "type"}
)
SCHEMA_ALLOWED_KEYWORDS = SCHEMA_ANNOTATION_KEYWORDS | SCHEMA_STRUCTURAL_KEYWORDS
SCHEMA_SCALAR_TYPES = frozenset({"boolean", "integer", "number", "string"})
SCHEMA_ALLOWED_TYPES = SCHEMA_SCALAR_TYPES | frozenset({"array", "object"})


class SchemaSubsetError(ValueError):
    """Raised when a schema falls outside the compiler-owned 03B subset."""


def validate_json_schema_subset(
    schema: Mapping[str, Any], *, field_name: str = "schema"
) -> MappingProxyType[str, Any]:
    """Validate, normalize, and deep-freeze an accepted JSON Schema subset."""
    normalized = normalize_json_schema(schema, field_name=field_name)
    return _freeze_json_object(normalized)


def normalize_json_schema(
    schema: Mapping[str, Any], *, field_name: str = "schema"
) -> dict[str, Any]:
    """Return a deterministic JSON-compatible schema with annotations stripped."""
    if not isinstance(schema, Mapping):
        raise SchemaSubsetError(f"{field_name} must be a JSON object")
    return _normalize_schema_node(schema, path=f"/{field_name}")


def normalized_schema_bytes(schema: Mapping[str, Any]) -> bytes:
    """Return stable UTF-8 bytes for semantically normalized accepted schemas."""
    normalized = normalize_json_schema(schema)
    return json.dumps(
        normalized,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def property_schema_compatibility_bytes(schema: Mapping[str, Any]) -> bytes:
    """Return stable bytes used for top-level argument property compatibility."""
    normalized = normalize_json_schema(schema, field_name="property_schema")
    return json.dumps(
        normalized,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _normalize_schema_node(schema: Mapping[str, Any], *, path: str) -> dict[str, Any]:
    _validate_object_keys(schema, path=path)
    if "const" in schema:
        return _normalize_const_only_schema(schema, path=path)
    schema_type = schema.get("type")
    if not isinstance(schema_type, str):
        raise SchemaSubsetError(f"{path}/type must be one schema type string")
    if schema_type not in SCHEMA_ALLOWED_TYPES:
        raise SchemaSubsetError(f"{path}/type {schema_type!r} is not supported")

    normalized: dict[str, Any] = {"type": schema_type}
    if "const" in schema:
        normalized["const"] = _normalize_scalar_literal(
            schema["const"], path=f"{path}/const"
        )
    if "enum" in schema:
        normalized["enum"] = _normalize_enum(schema["enum"], path=f"{path}/enum")

    if schema_type == "object":
        normalized.update(_normalize_object_schema(schema, path=path))
    elif schema_type == "array":
        normalized.update(_normalize_array_schema(schema, path=path))
    else:
        _reject_keywords(
            schema,
            {"properties", "required", "additionalProperties", "items"},
            path=path,
        )

    _validate_literal_types(normalized, path=path)
    return normalized


def _normalize_const_only_schema(
    schema: Mapping[str, Any], *, path: str
) -> dict[str, Any]:
    structural_keys = set(schema) & SCHEMA_STRUCTURAL_KEYWORDS
    if structural_keys != {"const"}:
        joined = ", ".join(sorted(structural_keys - {"const"}))
        raise SchemaSubsetError(
            f"{path}/const-only schema cannot include non-annotation keys"
            + (f": {joined}" if joined else "")
        )
    return {"const": _normalize_scalar_literal(schema["const"], path=f"{path}/const")}


def _normalize_object_schema(schema: Mapping[str, Any], *, path: str) -> dict[str, Any]:
    _reject_keywords(schema, {"items"}, path=path)
    additional_properties = schema.get("additionalProperties")
    if additional_properties is not False:
        raise SchemaSubsetError(f"{path}/additionalProperties must be false")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise SchemaSubsetError(f"{path}/properties must be an object")

    normalized_properties: dict[str, Any] = {}
    for property_name in sorted(properties):
        if not isinstance(property_name, str) or not property_name:
            raise SchemaSubsetError(f"{path}/properties keys must be nonblank strings")
        property_schema = properties[property_name]
        if not isinstance(property_schema, Mapping):
            raise SchemaSubsetError(
                f"{path}/properties/{property_name} must be a schema object"
            )
        normalized_properties[property_name] = _normalize_schema_node(
            property_schema, path=f"{path}/properties/{property_name}"
        )

    required = schema.get("required", [])
    if not isinstance(required, list | tuple):
        raise SchemaSubsetError(f"{path}/required must be an array")
    normalized_required: list[str] = []
    seen_required: set[str] = set()
    for index, item in enumerate(required):
        if not isinstance(item, str) or not item:
            raise SchemaSubsetError(
                f"{path}/required/{index} must be a nonblank string"
            )
        if item in seen_required:
            raise SchemaSubsetError(
                f"{path}/required contains duplicate field {item!r}"
            )
        if item not in normalized_properties:
            raise SchemaSubsetError(f"{path}/required field {item!r} is not a property")
        seen_required.add(item)
        normalized_required.append(item)

    return {
        "additionalProperties": False,
        "properties": normalized_properties,
        "required": sorted(normalized_required),
    }


def _normalize_array_schema(schema: Mapping[str, Any], *, path: str) -> dict[str, Any]:
    _reject_keywords(
        schema, {"properties", "required", "additionalProperties"}, path=path
    )
    items = schema.get("items")
    if not isinstance(items, Mapping):
        raise SchemaSubsetError(f"{path}/items must be a schema object")
    return {"items": _normalize_schema_node(items, path=f"{path}/items")}


def _normalize_enum(value: Any, *, path: str) -> list[str | int | float | bool | None]:
    if not isinstance(value, list | tuple) or not value:
        raise SchemaSubsetError(f"{path} must be a non-empty array")
    normalized: list[str | int | float | bool | None] = []
    seen: set[tuple[str, Any]] = set()
    for index, item in enumerate(value):
        literal = _normalize_scalar_literal(item, path=f"{path}/{index}")
        key = _scalar_uniqueness_key(literal)
        if key in seen:
            raise SchemaSubsetError(f"{path} contains duplicate scalar literal")
        seen.add(key)
        normalized.append(literal)
    return normalized


def _normalize_scalar_literal(
    value: Any, *, path: str
) -> str | int | float | bool | None:
    if value is None:
        return None
    if isinstance(value, bool | str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        json.dumps(value, allow_nan=False)
        return value
    raise SchemaSubsetError(f"{path} must be a scalar string, number, boolean, or null")


def _validate_literal_types(schema: Mapping[str, Any], *, path: str) -> None:
    schema_type = schema["type"]
    for key in ("const", "enum"):
        if key not in schema:
            continue
        values = schema[key] if key == "enum" else [schema[key]]
        for value in values:
            if not _literal_matches_type(value, schema_type):
                raise SchemaSubsetError(
                    f"{path}/{key} value does not match schema type"
                )


def _literal_matches_type(value: Any, schema_type: str) -> bool:
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if schema_type == "string":
        return isinstance(value, str)
    return False


def _validate_object_keys(schema: Mapping[str, Any], *, path: str) -> None:
    for key in schema:
        if not isinstance(key, str):
            raise SchemaSubsetError(f"{path} keywords must be strings")
        if key not in SCHEMA_ALLOWED_KEYWORDS:
            raise SchemaSubsetError(f"{path}/{key} is not in the accepted 03B subset")


def _reject_keywords(
    schema: Mapping[str, Any], keywords: set[str], *, path: str
) -> None:
    for keyword in sorted(keywords):
        if keyword in schema:
            raise SchemaSubsetError(
                f"{path}/{keyword} is not valid for type {schema['type']!r}"
            )


def _scalar_uniqueness_key(value: str | int | float | bool | None) -> tuple[str, Any]:
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, str):
        return ("string", value)
    return ("number", value)


def _freeze_json_object(value: Mapping[str, Any]) -> MappingProxyType[str, Any]:
    frozen = _freeze_json_value(dict(value))
    if not isinstance(frozen, MappingProxyType):
        raise SchemaSubsetError("schema must be a JSON object")
    return frozen


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json_value(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_json_value(item) for item in value)
    json.dumps(value, allow_nan=False)
    return value
