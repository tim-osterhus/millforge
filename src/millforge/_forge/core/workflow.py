"""Tool and workflow definitions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, create_model


_SCHEMA_OBJECT_KEYS = frozenset(
    {"type", "properties", "required", "description", "additionalProperties"}
)
_SCHEMA_PROPERTY_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "description",
        "default",
        "enum",
        "items",
        "additionalProperties",
    }
)
_SUPPORTED_JSON_TYPES = frozenset(
    {"string", "integer", "number", "boolean", "object", "array"}
)


def _to_pascal(name: str) -> str:
    """Convert snake_case tool name to PascalCaseParams."""
    return "".join(part.capitalize() for part in name.split("_")) + "Params"


def _reject_unsupported_keys(
    schema: dict[str, Any],
    *,
    allowed: frozenset[str],
    location: str,
) -> None:
    unsupported = sorted(set(schema) - allowed)
    if unsupported:
        raise ValueError(
            f"Unsupported JSON Schema feature(s) at {location}: "
            f"{', '.join(unsupported)}"
        )


def _require_object(value: Any, *, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Expected object at {location}")
    return value


def _validate_additional_properties(schema: dict[str, Any], *, location: str) -> None:
    if "additionalProperties" not in schema:
        raise ValueError(
            f"Missing required JSON Schema key at {location}: additionalProperties"
        )
    if schema["additionalProperties"] is not False:
        raise ValueError(
            "Unsupported JSON Schema feature at "
            f"{location}: additionalProperties must be false"
        )


def _validate_required(
    required: Any,
    properties: dict[str, Any],
    *,
    location: str,
) -> set[str]:
    if required is None:
        raise ValueError(f"Missing required JSON Schema key at {location}: required")
    if not isinstance(required, list) or not all(
        isinstance(item, str) for item in required
    ):
        raise ValueError(f"Expected string list for required at {location}")
    unknown = sorted(set(required) - set(properties))
    if unknown:
        raise ValueError(
            f"Required field(s) not present in properties at {location}: "
            f"{', '.join(unknown)}"
        )
    return set(required)


def _json_schema_to_type(
    prop: dict[str, Any],
    field_name: str,
    model_name_prefix: str,
) -> Any:
    """Convert a single JSON Schema property dict to a Python type.

    Handles primitives, enums, nested objects, and arrays recursively.
    """
    _reject_unsupported_keys(
        prop,
        allowed=_SCHEMA_PROPERTY_KEYS,
        location=f"{model_name_prefix}.{field_name}",
    )
    if "type" not in prop:
        raise ValueError(
            f"Missing JSON Schema type at {model_name_prefix}.{field_name}"
        )

    json_type_value = prop["type"]
    if not isinstance(json_type_value, str):
        raise ValueError(
            f"Unsupported JSON Schema type at {model_name_prefix}.{field_name}"
        )
    if json_type_value not in _SUPPORTED_JSON_TYPES:
        raise ValueError(
            f"Unsupported JSON Schema type at {model_name_prefix}.{field_name}: "
            f"{json_type_value}"
        )

    # Enum takes priority — Literal type
    if "enum" in prop:
        enum_values = prop["enum"]
        if not isinstance(enum_values, list) or not enum_values:
            raise ValueError(
                f"Expected non-empty enum list at {model_name_prefix}.{field_name}"
            )
        values = tuple(enum_values)
        return Literal[values]  # type: ignore[valid-type]

    type_map: dict[str, type] = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
    }

    if json_type_value in type_map:
        return type_map[json_type_value]

    if json_type_value == "object":
        _validate_additional_properties(
            prop,
            location=f"{model_name_prefix}.{field_name}",
        )
        if "properties" not in prop:
            raise ValueError(
                "Missing required JSON Schema key at "
                f"{model_name_prefix}.{field_name}: properties"
            )
        sub_props = _require_object(
            prop["properties"],
            location=f"{model_name_prefix}.{field_name}.properties",
        )
        sub_required = _validate_required(
            prop.get("required"),
            sub_props,
            location=f"{model_name_prefix}.{field_name}",
        )
        return _build_model(
            sub_props,
            sub_required,
            f"{model_name_prefix}_{field_name.capitalize()}",
        )

    if json_type_value == "array":
        if "items" not in prop:
            raise ValueError(
                f"Array schema missing items at {model_name_prefix}.{field_name}"
            )
        items = _require_object(
            prop["items"],
            location=f"{model_name_prefix}.{field_name}.items",
        )
        item_type = _json_schema_to_type(items, field_name + "Item", model_name_prefix)
        return list[item_type]  # type: ignore[valid-type]

    raise AssertionError(f"unreachable JSON Schema type: {json_type_value}")


def _build_model(
    properties: dict[str, Any],
    required: set[str],
    model_name: str,
) -> type[BaseModel]:
    """Build a dynamic Pydantic model from JSON Schema properties."""
    fields: dict[str, Any] = {}

    for fname, fprop in properties.items():
        if not isinstance(fname, str):
            raise ValueError(f"Unsupported non-string JSON Schema property: {fname!r}")
        fprop = _require_object(fprop, location=f"{model_name}.{fname}")
        python_type = _json_schema_to_type(fprop, fname, model_name)
        description = fprop.get("description")
        default = fprop.get("default")

        if fname in required:
            if description is not None:
                fields[fname] = (python_type, Field(description=description))
            else:
                fields[fname] = (python_type, ...)
        else:
            # Optional field
            if default is not None:
                if description is not None:
                    fields[fname] = (
                        python_type | None,
                        Field(default=default, description=description),
                    )
                else:
                    fields[fname] = (python_type | None, Field(default=default))
            else:
                if description is not None:
                    fields[fname] = (
                        python_type | None,
                        Field(default=None, description=description),
                    )
                else:
                    fields[fname] = (python_type | None, None)

    return create_model(
        model_name,
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )  # type: ignore[call-overload]


class ToolSpec(BaseModel):
    """Declarative tool schema — what the LLM sees."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    parameters: type[BaseModel]

    @classmethod
    def from_json_schema(
        cls,
        name: str,
        description: str,
        schema: dict[str, Any],
    ) -> ToolSpec:
        """Create a ToolSpec from a raw JSON Schema dict.

        The *schema* argument is the ``parameters`` object from an OpenAI-style
        tool definition (i.e. a JSON Schema with ``properties``, ``required``,
        etc.).
        """
        _reject_unsupported_keys(
            schema,
            allowed=_SCHEMA_OBJECT_KEYS,
            location=f"{name} parameters",
        )
        _validate_additional_properties(schema, location=f"{name} parameters")
        if schema.get("type") != "object":
            raise ValueError("Tool parameter schema must be a JSON object")
        if "properties" not in schema:
            raise ValueError(
                f"Missing required JSON Schema key at {name} parameters: properties"
            )
        properties = _require_object(
            schema["properties"],
            location=f"{name} parameters.properties",
        )
        required = _validate_required(
            schema.get("required"),
            properties,
            location=f"{name} parameters",
        )
        model_name = _to_pascal(name)
        params_cls = _build_model(properties, required, model_name)
        return cls(name=name, description=description, parameters=params_cls)

    def get_json_schema(self) -> dict[str, Any]:
        """Return JSON Schema dict for this tool's parameters."""
        return self.parameters.model_json_schema()


@dataclass
class ToolDef:
    """Binds a tool schema to its implementation.

    Downstream projects define tools as ToolDefs. The Workflow holds these
    in a dict keyed by name, deriving the spec list (for the LLM) and
    callable lookup (for execution) internally.

    Prerequisites express conditional dependencies: "if you call this tool,
    you must have called tool X first." Entries can be:
    - str: name-only ("read_file" — any prior call to read_file satisfies it)
    - dict: arg-matched ({"tool": "read_file", "match_arg": "path"} — a prior
      call to read_file with the same ``path`` value satisfies it)
    - dict: mapped arg-matched
      ({"tool": "lookup", "prerequisite_arg": "source", "current_arg": "path"} —
      a prior lookup ``source`` value must equal the current ``path`` value)
    """

    spec: ToolSpec
    callable: Callable[..., Any]
    prerequisites: list[str | dict[str, str]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.spec.name


@dataclass
class ToolCall:
    """Tool invocation returned by an LLMClient.

    ``args`` is *not* validated at construction. ResponseValidator enforces
    args-shape (must be a dict) before the call reaches downstream stages
    that read into it. Treating args-shape uniformly with other validator
    checks (unknown tool name) lets a malformed call ride the canonical
    tool-error channel instead of crashing the parser.
    """

    tool: str
    args: Any  # may be a non-dict when malformed; ResponseValidator rejects shape
    reasoning: str | None = None


@dataclass
class TextResponse:
    """Non-tool-call response from the model (reasoning trace, refusal, etc.)."""

    content: str


LLMResponse: TypeAlias = list[ToolCall] | TextResponse


@dataclass
class Workflow:
    """Declarative workflow definition. Provided by downstream projects.

    The Workflow holds ToolDefs in an ordered dict keyed by tool name.
    Keys must match ToolDef.spec.name — validated at construction time.
    It does NOT contain execution logic — that's the WorkflowRunner's job.
    """

    name: str
    description: str
    tools: dict[str, ToolDef]
    required_steps: list[str]
    terminal_tool: str | list[str]
    system_prompt_template: str
    terminal_tools: frozenset[str] = field(default_factory=frozenset, init=False)

    def __post_init__(self) -> None:
        # Normalize terminal_tool to frozenset for O(1) membership checks.
        if isinstance(self.terminal_tool, str):
            self.terminal_tools = frozenset([self.terminal_tool])
        else:
            self.terminal_tools = frozenset(self.terminal_tool)

        for key, tool_def in self.tools.items():
            if key != tool_def.name:
                raise ValueError(
                    f"Tool key '{key}' does not match ToolDef name '{tool_def.name}'"
                )
        tool_names = set(self.tools.keys())
        for step in self.required_steps:
            if step not in tool_names:
                raise ValueError(f"Required step '{step}' not in tools: {tool_names}")
        for tt in self.terminal_tools:
            if tt not in tool_names:
                raise ValueError(f"Terminal tool '{tt}' not in tools: {tool_names}")
            if tt in self.required_steps:
                raise ValueError(f"Terminal tool '{tt}' cannot also be a required step")
        for key, tool_def in self.tools.items():
            for prereq in tool_def.prerequisites:
                prereq_name = prereq if isinstance(prereq, str) else prereq["tool"]
                if prereq_name not in tool_names:
                    raise ValueError(
                        f"Prerequisite '{prereq_name}' for tool '{key}' "
                        f"not in tools: {tool_names}"
                    )

    def build_system_prompt(self, **kwargs: str) -> str:
        """Render the system prompt with user-provided values."""
        return self.system_prompt_template.format(**kwargs)

    def get_tool_specs(self) -> list[ToolSpec]:
        """Return all tool specs for passing to the LLM client."""
        return [t.spec for t in self.tools.values()]

    def get_callable(self, tool_name: str) -> Callable[..., Any]:
        """Return the callable for a tool by name. Raises KeyError if not found."""
        return self.tools[tool_name].callable
