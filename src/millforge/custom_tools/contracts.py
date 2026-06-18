"""Frozen custom-tool source, policy, compilation, and diagnostic contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from millforge import IdempotencyClass, SideEffectClass, canonical_json_serialize
from millforge.compiler.diagnostics import detect_secret_candidate
from millforge.compiler.schema_validation import normalize_json_schema
from millforge.compiler.validators import (
    validate_artifact_id,
    validate_capability_id,
    validate_canonical_tool_id,
    validate_nonblank,
    validate_sha256,
    validate_tool_version,
    validate_unique,
    validate_utf8_size,
)
from millforge.contracts import RedactionPolicy
from millforge.custom_tools.diagnostics import (
    CustomToolDiagnostic,
    CustomToolDiagnosticCode,
    CustomToolDiagnosticPhase,
    custom_tool_diagnostic_sort_key,
    malformed_input_diagnostic,
)
from millforge.tools.registry import ToolDescriptor, ToolOutputPolicy, ToolTimeoutPolicy

CUSTOM_TOOL_DECLARATION_HASH_KIND = "millforge.custom_tool.declaration.v1"
CUSTOM_TOOL_SOURCE_HASH_KIND = "millforge.custom_tool.source.v1"
CUSTOM_TOOL_COMPILATION_RECORD_HASH_KIND = "millforge.custom_tool.compilation_record.v1"
CUSTOM_TOOL_SOURCE_SCHEMA = "millforge.custom_tool.source"
CUSTOM_TOOL_SOURCE_KIND = "custom_tool_source"
CUSTOM_TOOL_SOURCE_VERSION = "1.0"

MAX_CUSTOM_TOOL_FIELD_UTF8 = 512
MAX_CUSTOM_TOOL_PACKAGE_ID_UTF8 = 160
MAX_CUSTOM_TOOL_DESCRIPTION_UTF8 = 65_536
MAX_CUSTOM_TOOL_SCHEMA_BYTES = 65_536
MAX_CUSTOM_TOOL_COUNT = 128

JsonValue = Any

_SIDE_EFFECTS_REQUIRING_APPROVAL = frozenset(
    item for item in SideEffectClass if item is not SideEffectClass.READ_ONLY
)


class CustomToolRuntimeKind(str, Enum):
    """Closed custom-tool runtime kind values for 05C contracts."""

    CONTRACT_ONLY = "contract_only"


class CustomToolDescriptionPolicy(str, Enum):
    """Closed description provenance policy values."""

    OPERATOR_SUPPLIED = "operator_supplied"
    SOURCE_SUPPLIED = "source_supplied"


class CustomToolApprovalPolicy(str, Enum):
    """Closed approval policy values for custom-tool declarations."""

    NONE = "none"
    MILLRACE_EXPLICIT = "millrace_explicit"
    OPERATOR_OUT_OF_BAND = "operator_out_of_band"
    FORBIDDEN = "forbidden"


class CustomToolInputPolicy(str, Enum):
    """Closed input acceptance policy values for later compilation."""

    JSON_SCHEMA_EXACT = "json_schema_exact"


class CustomToolOutputPolicy(str, Enum):
    """Closed output acceptance policy values for later compilation."""

    JSON_SCHEMA_EXACT = "json_schema_exact"


class CustomToolDeclaration(BaseModel):
    """Immutable source declaration for one compile-only custom tool."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        hide_input_in_errors=True,
        use_enum_values=False,
    )

    tool_id: StrictStr
    tool_version: StrictInt = Field(ge=1)
    implementation_id: StrictStr
    runtime_kind: CustomToolRuntimeKind = CustomToolRuntimeKind.CONTRACT_ONLY
    model_tool_name: StrictStr
    description: StrictStr
    description_policy: CustomToolDescriptionPolicy
    input_schema: Mapping[str, JsonValue]
    output_schema: Mapping[str, JsonValue]
    required_capabilities: tuple[StrictStr, ...] = Field(default_factory=tuple)
    produced_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    side_effect_class: SideEffectClass
    idempotency: IdempotencyClass
    timeout_policy: ToolTimeoutPolicy
    output_policy: ToolOutputPolicy
    approval_policy: CustomToolApprovalPolicy
    input_policy: CustomToolInputPolicy = CustomToolInputPolicy.JSON_SCHEMA_EXACT
    output_contract_policy: CustomToolOutputPolicy = (
        CustomToolOutputPolicy.JSON_SCHEMA_EXACT
    )
    idempotency_key_policy: StrictStr | None = None
    expected_declaration_sha256: StrictStr | None = None
    expected_descriptor_sha256: StrictStr | None = None
    expected_compilation_record_sha256: StrictStr | None = None

    _diagnostic_phase: ClassVar[CustomToolDiagnosticPhase] = (
        CustomToolDiagnosticPhase.DECLARATION
    )

    @classmethod
    def validate_contract(cls, raw: Any) -> CustomToolContractValidation:
        """Validate raw input and return stable diagnostics instead of exception text."""
        return _validate_contract_for_model(cls, raw)

    @field_validator("tool_id")
    @classmethod
    def _tool_id_valid(cls, value: str) -> str:
        return validate_canonical_tool_id(value)

    @field_validator("tool_version")
    @classmethod
    def _tool_version_valid(cls, value: int) -> int:
        return validate_tool_version(value)

    @field_validator("implementation_id", "model_tool_name")
    @classmethod
    def _identity_text_valid(cls, value: str, info: Any) -> str:
        return _safe_custom_tool_string(
            value, info.field_name, MAX_CUSTOM_TOOL_FIELD_UTF8
        )

    @field_validator("description")
    @classmethod
    def _description_valid(cls, value: str) -> str:
        return _safe_custom_tool_string(
            value, "description", MAX_CUSTOM_TOOL_DESCRIPTION_UTF8
        )

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _schema_frozen(cls, value: Any, info: Any) -> MappingProxyType[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{info.field_name} must be a JSON object")
        _reject_secret_values(value, path=f"/{info.field_name}")
        normalized = normalize_json_schema(value, field_name=info.field_name)
        _validate_schema_size(normalized, info.field_name)
        return _freeze_json_object(normalized)

    @field_validator("required_capabilities", "produced_artifact_ids", mode="before")
    @classmethod
    def _string_tuple_snapshot(cls, value: Any, info: Any) -> tuple[str, ...]:
        return _sorted_string_tuple(value, info.field_name)

    @field_validator("required_capabilities")
    @classmethod
    def _capabilities_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for capability_id in value:
            validate_capability_id(capability_id)
        return validate_unique(value, "required_capabilities")

    @field_validator("produced_artifact_ids")
    @classmethod
    def _produced_artifacts_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for artifact_id in value:
            validate_artifact_id(artifact_id)
        return validate_unique(value, "produced_artifact_ids")

    @field_validator("idempotency_key_policy")
    @classmethod
    def _idempotency_key_policy_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _safe_custom_tool_string(
            value, "idempotency_key_policy", MAX_CUSTOM_TOOL_FIELD_UTF8
        )

    @field_validator(
        "expected_declaration_sha256",
        "expected_descriptor_sha256",
        "expected_compilation_record_sha256",
    )
    @classmethod
    def _expected_hash_valid(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return validate_sha256(value, info.field_name)

    @model_validator(mode="after")
    def _declaration_semantics_valid(self) -> CustomToolDeclaration:
        if self.approval_policy is CustomToolApprovalPolicy.FORBIDDEN:
            raise ValueError("forbidden approval policy cannot compile a tool")
        if (
            self.side_effect_class in _SIDE_EFFECTS_REQUIRING_APPROVAL
            and self.approval_policy
            not in {
                CustomToolApprovalPolicy.MILLRACE_EXPLICIT,
                CustomToolApprovalPolicy.OPERATOR_OUT_OF_BAND,
            }
        ):
            raise ValueError("side-effecting custom tools require explicit approval")
        if (
            self.side_effect_class in _SIDE_EFFECTS_REQUIRING_APPROVAL
            or self.produced_artifact_ids
        ) and not self.required_capabilities:
            raise ValueError(
                "side-effecting or artifact-producing custom tools require capabilities"
            )
        return self

    @property
    def input_schema_sha256(self) -> str:
        return _sha256_hex(_thaw_json_value(self.input_schema))

    @property
    def output_schema_sha256(self) -> str:
        return _sha256_hex(_thaw_json_value(self.output_schema))

    @property
    def declaration_sha256(self) -> str:
        return _sha256_hex(
            {
                "kind": CUSTOM_TOOL_DECLARATION_HASH_KIND,
                "tool_id": self.tool_id,
                "tool_version": self.tool_version,
                "implementation_id": self.implementation_id,
                "runtime_kind": self.runtime_kind.value,
                "model_tool_name": self.model_tool_name,
                "description": self.description,
                "description_policy": self.description_policy.value,
                "input_schema": _thaw_json_value(self.input_schema),
                "output_schema": _thaw_json_value(self.output_schema),
                "required_capabilities": list(self.required_capabilities),
                "produced_artifact_ids": list(self.produced_artifact_ids),
                "side_effect_class": self.side_effect_class.value,
                "idempotency": self.idempotency.value,
                "timeout_policy": self.timeout_policy.model_dump(mode="json"),
                "output_policy": self.output_policy.model_dump(mode="json"),
                "approval_policy": self.approval_policy.value,
                "input_policy": self.input_policy.value,
                "output_contract_policy": self.output_contract_policy.value,
                "idempotency_key_policy": self.idempotency_key_policy,
            }
        )

    @field_serializer("input_schema", "output_schema")
    def _serialize_schema(self, value: Any) -> Any:
        return _thaw_json_value(value)


class CustomToolSourceManifest(BaseModel):
    """Immutable custom-tool source manifest for deterministic offline compilation."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        hide_input_in_errors=True,
    )

    schema_version: StrictStr = CUSTOM_TOOL_SOURCE_SCHEMA
    kind: StrictStr = CUSTOM_TOOL_SOURCE_KIND
    version: StrictStr = CUSTOM_TOOL_SOURCE_VERSION
    package_id: StrictStr
    package_version: StrictInt = Field(ge=1)
    source_name: StrictStr | None = None
    created_at: StrictStr
    tools: tuple[CustomToolDeclaration, ...]
    policy_metadata: Mapping[str, JsonValue] = Field(
        default_factory=lambda: MappingProxyType({})
    )
    expected_source_sha256: StrictStr | None = None

    _diagnostic_phase: ClassVar[CustomToolDiagnosticPhase] = (
        CustomToolDiagnosticPhase.SOURCE
    )

    @classmethod
    def validate_contract(cls, raw: Any) -> CustomToolContractValidation:
        """Validate raw input and return stable diagnostics instead of exception text."""
        return _validate_contract_for_model(cls, raw)

    @field_validator("schema_version")
    @classmethod
    def _schema_version_valid(cls, value: str) -> str:
        if value != CUSTOM_TOOL_SOURCE_SCHEMA:
            raise ValueError(
                "schema_version must identify custom-tool source manifests"
            )
        return value

    @field_validator("kind")
    @classmethod
    def _kind_valid(cls, value: str) -> str:
        if value != CUSTOM_TOOL_SOURCE_KIND:
            raise ValueError("kind must be custom_tool_source")
        return value

    @field_validator("version")
    @classmethod
    def _version_valid(cls, value: str) -> str:
        if value != CUSTOM_TOOL_SOURCE_VERSION:
            raise ValueError("version must be 1.0")
        return value

    @field_validator("package_id", "source_name")
    @classmethod
    def _package_text_valid(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        maximum = (
            MAX_CUSTOM_TOOL_PACKAGE_ID_UTF8
            if info.field_name == "package_id"
            else MAX_CUSTOM_TOOL_FIELD_UTF8
        )
        return _safe_custom_tool_string(value, info.field_name, maximum)

    @field_validator("created_at")
    @classmethod
    def _created_at_valid(cls, value: str) -> str:
        return _validate_utc_timestamp(value, "created_at")

    @field_validator("tools", mode="before")
    @classmethod
    def _tools_tuple(cls, value: Any) -> tuple[Any, ...]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("tools must be an array")
        return tuple(value)

    @field_validator("policy_metadata")
    @classmethod
    def _policy_metadata_frozen(cls, value: Any) -> MappingProxyType[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("policy_metadata must be a JSON object")
        _reject_secret_values(value, path="/policy_metadata")
        return _freeze_json_object(value)

    @field_validator("expected_source_sha256")
    @classmethod
    def _expected_source_hash_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_sha256(value, "expected_source_sha256")

    @model_validator(mode="after")
    def _source_consistent(self) -> CustomToolSourceManifest:
        if not self.tools:
            raise ValueError("tools must contain at least one tool")
        if len(self.tools) > MAX_CUSTOM_TOOL_COUNT:
            raise ValueError("tools exceeds maximum custom-tool count")
        tool_keys = tuple(f"{tool.tool_id}@{tool.tool_version}" for tool in self.tools)
        validate_unique(tool_keys, "custom tool identities")
        validate_unique(
            tuple(tool.model_tool_name for tool in self.tools),
            "custom tool model_tool_name",
        )
        validate_unique(
            tuple(tool.implementation_id for tool in self.tools),
            "custom tool implementation_id",
        )
        return self

    @property
    def source_sha256(self) -> str:
        return _sha256_hex(
            {
                "kind": CUSTOM_TOOL_SOURCE_HASH_KIND,
                "schema_version": self.schema_version,
                "source_kind": self.kind,
                "version": self.version,
                "package_id": self.package_id,
                "package_version": self.package_version,
                "source_name": self.source_name,
                "tools": [
                    {
                        "tool_id": tool.tool_id,
                        "tool_version": tool.tool_version,
                        "declaration_sha256": tool.declaration_sha256,
                    }
                    for tool in sorted(
                        self.tools, key=lambda item: (item.tool_id, item.tool_version)
                    )
                ],
                "policy_metadata": _thaw_json_value(self.policy_metadata),
            }
        )

    @field_serializer("policy_metadata")
    def _serialize_policy_metadata(self, value: Any) -> Any:
        return _thaw_json_value(value)


class CustomToolCompilerPolicy(BaseModel):
    """Closed policy constraints for deterministic custom-tool compilation."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        hide_input_in_errors=True,
    )

    allowed_capability_ids: tuple[StrictStr, ...]
    allowed_runtime_kinds: tuple[CustomToolRuntimeKind, ...] = (
        CustomToolRuntimeKind.CONTRACT_ONLY,
    )
    max_tools: StrictInt = Field(
        default=MAX_CUSTOM_TOOL_COUNT, ge=1, le=MAX_CUSTOM_TOOL_COUNT
    )
    max_schema_bytes: StrictInt = Field(default=MAX_CUSTOM_TOOL_SCHEMA_BYTES, ge=1)
    max_description_utf8: StrictInt = Field(
        default=MAX_CUSTOM_TOOL_DESCRIPTION_UTF8,
        ge=1,
        le=MAX_CUSTOM_TOOL_DESCRIPTION_UTF8,
    )
    require_expected_hashes: StrictBool = False
    side_effect_approval_matrix: Mapping[
        SideEffectClass, tuple[CustomToolApprovalPolicy, ...]
    ] = Field(default_factory=lambda: MappingProxyType(_default_approval_matrix()))

    _diagnostic_phase: ClassVar[CustomToolDiagnosticPhase] = (
        CustomToolDiagnosticPhase.POLICY
    )

    @classmethod
    def validate_contract(cls, raw: Any) -> CustomToolContractValidation:
        """Validate raw input and return stable diagnostics instead of exception text."""
        return _validate_contract_for_model(cls, raw)

    @field_validator("allowed_capability_ids", mode="before")
    @classmethod
    def _allowed_capabilities_snapshot(cls, value: Any) -> tuple[str, ...]:
        values = _sorted_string_tuple(value, "allowed_capability_ids")
        for capability_id in values:
            validate_capability_id(capability_id)
        return validate_unique(values, "allowed_capability_ids")

    @field_validator("allowed_runtime_kinds", mode="before")
    @classmethod
    def _runtime_kinds_tuple(cls, value: Any) -> tuple[Any, ...]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("allowed_runtime_kinds must be an array")
        return tuple(value)

    @field_validator("allowed_runtime_kinds")
    @classmethod
    def _runtime_kinds_valid(
        cls, value: tuple[CustomToolRuntimeKind, ...]
    ) -> tuple[CustomToolRuntimeKind, ...]:
        if value != (CustomToolRuntimeKind.CONTRACT_ONLY,):
            raise ValueError(
                "allowed runtime kinds are initially limited to contract_only"
            )
        return value

    @field_validator("side_effect_approval_matrix")
    @classmethod
    def _matrix_frozen(
        cls, value: Any
    ) -> MappingProxyType[SideEffectClass, tuple[CustomToolApprovalPolicy, ...]]:
        if not isinstance(value, Mapping):
            raise ValueError("side_effect_approval_matrix must be an object")
        matrix: dict[SideEffectClass, tuple[CustomToolApprovalPolicy, ...]] = {}
        for raw_key, raw_values in value.items():
            key = (
                raw_key
                if isinstance(raw_key, SideEffectClass)
                else SideEffectClass(raw_key)
            )
            if isinstance(raw_values, str) or not isinstance(raw_values, (list, tuple)):
                raise ValueError("side_effect_approval_matrix values must be arrays")
            policies = tuple(
                item
                if isinstance(item, CustomToolApprovalPolicy)
                else CustomToolApprovalPolicy(item)
                for item in raw_values
            )
            if CustomToolApprovalPolicy.FORBIDDEN in policies:
                raise ValueError("forbidden approval policy is denial evidence only")
            if (
                key in _SIDE_EFFECTS_REQUIRING_APPROVAL
                and CustomToolApprovalPolicy.NONE in policies
            ):
                raise ValueError("side-effecting custom tools cannot use approval none")
            matrix[key] = policies
        return MappingProxyType(
            dict(sorted(matrix.items(), key=lambda item: item[0].value))
        )

    @field_serializer("side_effect_approval_matrix")
    def _serialize_matrix(
        self,
        value: MappingProxyType[SideEffectClass, tuple[CustomToolApprovalPolicy, ...]],
    ) -> dict[str, list[str]]:
        return {
            side_effect.value: [policy.value for policy in policies]
            for side_effect, policies in sorted(
                value.items(), key=lambda item: item[0].value
            )
        }


class CustomToolCompilationRecord(BaseModel):
    """Immutable record for one contract-only compiled custom-tool declaration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    package_id: StrictStr
    package_version: StrictInt = Field(ge=1)
    source_sha256: StrictStr
    tool_id: StrictStr
    tool_version: StrictInt = Field(ge=1)
    implementation_id: StrictStr
    runtime_kind: CustomToolRuntimeKind
    model_tool_name: StrictStr
    declaration_sha256: StrictStr
    input_schema_sha256: StrictStr
    output_schema_sha256: StrictStr
    descriptor_sha256: StrictStr
    required_capabilities: tuple[StrictStr, ...]
    produced_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    side_effect_class: SideEffectClass
    idempotency: IdempotencyClass
    timeout_policy: ToolTimeoutPolicy
    output_policy: ToolOutputPolicy
    approval_policy: CustomToolApprovalPolicy
    compilation_record_sha256: StrictStr = ""

    @field_validator(
        "source_sha256",
        "declaration_sha256",
        "input_schema_sha256",
        "output_schema_sha256",
        "descriptor_sha256",
        "compilation_record_sha256",
    )
    @classmethod
    def _hash_valid(cls, value: str, info: Any) -> str:
        if info.field_name == "compilation_record_sha256" and value == "":
            return value
        return validate_sha256(value, info.field_name)

    @field_validator("tool_id")
    @classmethod
    def _tool_id_valid(cls, value: str) -> str:
        return validate_canonical_tool_id(value)

    @field_validator("tool_version")
    @classmethod
    def _tool_version_valid(cls, value: int) -> int:
        return validate_tool_version(value)

    @field_validator("required_capabilities", "produced_artifact_ids", mode="before")
    @classmethod
    def _string_tuple_snapshot(cls, value: Any, info: Any) -> tuple[str, ...]:
        return _sorted_string_tuple(value, info.field_name)

    @field_validator("required_capabilities")
    @classmethod
    def _capabilities_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for capability_id in value:
            validate_capability_id(capability_id)
        return validate_unique(value, "required_capabilities")

    @field_validator("produced_artifact_ids")
    @classmethod
    def _produced_artifacts_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for artifact_id in value:
            validate_artifact_id(artifact_id)
        return validate_unique(value, "produced_artifact_ids")

    @model_validator(mode="after")
    def _record_hash_consistent(self) -> CustomToolCompilationRecord:
        computed = _compilation_record_hash(self)
        if self.compilation_record_sha256 == "":
            object.__setattr__(self, "compilation_record_sha256", computed)
        elif self.compilation_record_sha256 != computed:
            raise ValueError("compilation_record_sha256 must match record contents")
        return self


class CustomToolCompilationResult(BaseModel):
    """Deterministic result container for custom-tool compilation attempts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    source_sha256: StrictStr | None = None
    descriptors: tuple[ToolDescriptor, ...] = Field(default_factory=tuple)
    records: tuple[CustomToolCompilationRecord, ...] = Field(default_factory=tuple)
    diagnostics: tuple[CustomToolDiagnostic, ...] = Field(default_factory=tuple)

    _diagnostic_phase: ClassVar[CustomToolDiagnosticPhase] = (
        CustomToolDiagnosticPhase.COMPILATION
    )

    @classmethod
    def validate_contract(cls, raw: Any) -> CustomToolContractValidation:
        """Validate raw input and return stable diagnostics instead of exception text."""
        return _validate_contract_for_model(cls, raw)

    @field_validator("source_sha256")
    @classmethod
    def _source_hash_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_sha256(value, "source_sha256")

    @model_validator(mode="after")
    def _result_consistent(self) -> CustomToolCompilationResult:
        if self.accepted:
            if self.diagnostics:
                raise ValueError(
                    "accepted compilation results cannot contain diagnostics"
                )
            if self.source_sha256 is None:
                raise ValueError("accepted compilation results require source hash")
            if not self.descriptors:
                raise ValueError("accepted compilation results require descriptors")
            if len(self.descriptors) != len(self.records):
                raise ValueError(
                    "accepted compilation results require one record per descriptor"
                )
            descriptor_by_hash = {
                descriptor.descriptor_sha256: descriptor
                for descriptor in self.descriptors
            }
            record_by_hash = {
                record.descriptor_sha256: record for record in self.records
            }
            if (
                len(descriptor_by_hash) != len(self.descriptors)
                or len(record_by_hash) != len(self.records)
                or descriptor_by_hash.keys() != record_by_hash.keys()
            ):
                raise ValueError("compilation records must match descriptor hashes")
            lowered = tuple(
                sorted(
                    (
                        (descriptor, record_by_hash[descriptor.descriptor_sha256])
                        for descriptor in descriptor_by_hash.values()
                    ),
                    key=lambda item: _accepted_result_identity_key(item[0], item[1]),
                )
            )
            object.__setattr__(
                self, "descriptors", tuple(descriptor for descriptor, _ in lowered)
            )
            object.__setattr__(self, "records", tuple(record for _, record in lowered))
        elif self.descriptors or self.records:
            raise ValueError(
                "rejected compilation results cannot contain descriptors or records"
            )
        else:
            object.__setattr__(
                self,
                "diagnostics",
                tuple(sorted(self.diagnostics, key=custom_tool_diagnostic_sort_key)),
            )
        return self


def _compilation_record_hash(record: CustomToolCompilationRecord) -> str:
    return _sha256_hex(
        {
            "kind": CUSTOM_TOOL_COMPILATION_RECORD_HASH_KIND,
            "package_id": record.package_id,
            "package_version": record.package_version,
            "source_sha256": record.source_sha256,
            "tool_id": record.tool_id,
            "tool_version": record.tool_version,
            "implementation_id": record.implementation_id,
            "runtime_kind": record.runtime_kind.value,
            "model_tool_name": record.model_tool_name,
            "declaration_sha256": record.declaration_sha256,
            "input_schema_sha256": record.input_schema_sha256,
            "output_schema_sha256": record.output_schema_sha256,
            "descriptor_sha256": record.descriptor_sha256,
            "required_capabilities": list(record.required_capabilities),
            "produced_artifact_ids": list(record.produced_artifact_ids),
            "side_effect_class": record.side_effect_class.value,
            "idempotency": record.idempotency.value,
            "timeout_policy": record.timeout_policy.model_dump(mode="json"),
            "output_policy": record.output_policy.model_dump(mode="json"),
            "approval_policy": record.approval_policy.value,
        }
    )


def _accepted_result_identity_key(
    descriptor: ToolDescriptor,
    record: CustomToolCompilationRecord,
) -> tuple[str, str, int, str, str, str, str]:
    return (
        record.package_id,
        descriptor.tool_id,
        descriptor.tool_version,
        descriptor.model_tool_name,
        descriptor.implementation_id,
        descriptor.descriptor_sha256,
        record.compilation_record_sha256,
    )


class CustomToolContractValidation(BaseModel):
    """Contract validation result for malformed raw custom-tool inputs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Any | None = None
    diagnostics: tuple[CustomToolDiagnostic, ...] = Field(default_factory=tuple)

    @property
    def accepted(self) -> bool:
        return self.value is not None and not self.diagnostics


class CustomToolContractModel(BaseModel):
    """Base helper for diagnostic validation of raw custom-tool mappings."""

    model_config = ConfigDict(frozen=True)
    _diagnostic_phase: ClassVar[CustomToolDiagnosticPhase] = (
        CustomToolDiagnosticPhase.DIAGNOSTIC
    )

    @classmethod
    def validate_contract(cls, raw: Any) -> CustomToolContractValidation:
        """Validate raw input and return stable diagnostics instead of exception text."""
        try:
            return CustomToolContractValidation(value=cls.model_validate(raw))
        except ValidationError as exc:
            missing_field = _validation_missing_field(exc)
            return CustomToolContractValidation(
                diagnostics=(
                    malformed_input_diagnostic(
                        phase=cls._diagnostic_phase,
                        model_name=cls.__name__,
                        path=_validation_pointer(exc),
                        missing_field=missing_field,
                        code=_validation_code(exc),
                    ),
                )
            )
        except Exception:
            return CustomToolContractValidation(
                diagnostics=(
                    malformed_input_diagnostic(
                        phase=cls._diagnostic_phase,
                        model_name=cls.__name__,
                        code=CustomToolDiagnosticCode.SOURCE_INVALID,
                    ),
                )
            )


def _validate_contract_for_model(
    cls: type[BaseModel], raw: Any
) -> CustomToolContractValidation:
    phase = getattr(cls, "_diagnostic_phase", CustomToolDiagnosticPhase.DIAGNOSTIC)
    try:
        return CustomToolContractValidation(value=cls.model_validate(raw))
    except ValidationError as exc:
        missing_field = _validation_missing_field(exc)
        return CustomToolContractValidation(
            diagnostics=(
                malformed_input_diagnostic(
                    phase=phase,
                    model_name=cls.__name__,
                    path=_validation_pointer(exc),
                    missing_field=missing_field,
                    code=_validation_code(exc),
                ),
            )
        )
    except Exception:
        return CustomToolContractValidation(
            diagnostics=(
                malformed_input_diagnostic(
                    phase=phase,
                    model_name=cls.__name__,
                    code=CustomToolDiagnosticCode.SOURCE_INVALID,
                ),
            )
        )


def compilation_record_from_declaration(
    source: CustomToolSourceManifest,
    declaration: CustomToolDeclaration,
    descriptor: ToolDescriptor | None = None,
) -> CustomToolCompilationRecord:
    """Create deterministic record metadata for a contract-only declaration."""
    descriptor = descriptor or tool_descriptor_from_declaration(declaration)
    _validate_descriptor_matches_declaration(declaration, descriptor)
    return CustomToolCompilationRecord(
        package_id=source.package_id,
        package_version=source.package_version,
        source_sha256=source.source_sha256,
        tool_id=declaration.tool_id,
        tool_version=declaration.tool_version,
        implementation_id=declaration.implementation_id,
        runtime_kind=declaration.runtime_kind,
        model_tool_name=declaration.model_tool_name,
        declaration_sha256=declaration.declaration_sha256,
        input_schema_sha256=declaration.input_schema_sha256,
        output_schema_sha256=declaration.output_schema_sha256,
        descriptor_sha256=descriptor.descriptor_sha256,
        required_capabilities=declaration.required_capabilities,
        produced_artifact_ids=declaration.produced_artifact_ids,
        side_effect_class=declaration.side_effect_class,
        idempotency=declaration.idempotency,
        timeout_policy=declaration.timeout_policy,
        output_policy=declaration.output_policy,
        approval_policy=declaration.approval_policy,
    )


def tool_descriptor_from_declaration(
    declaration: CustomToolDeclaration,
) -> ToolDescriptor:
    """Create descriptor data for a contract-only declaration without registration."""
    return ToolDescriptor(
        tool_id=declaration.tool_id,
        tool_version=declaration.tool_version,
        implementation_id=declaration.implementation_id,
        model_tool_name=declaration.model_tool_name,
        description=declaration.description,
        input_schema=cast(MappingProxyType[str, JsonValue], declaration.input_schema),
        output_schema=cast(MappingProxyType[str, JsonValue], declaration.output_schema),
        required_capabilities=declaration.required_capabilities,
        produced_artifact_ids=declaration.produced_artifact_ids,
        side_effect_class=declaration.side_effect_class,
        idempotency=declaration.idempotency,
        timeout_policy=declaration.timeout_policy,
        output_policy=declaration.output_policy,
    )


def _validate_descriptor_matches_declaration(
    declaration: CustomToolDeclaration,
    descriptor: ToolDescriptor,
) -> None:
    expected = tool_descriptor_from_declaration(declaration)
    expected_dump = expected.model_dump(mode="json")
    supplied_dump = descriptor.model_dump(mode="json")
    if supplied_dump != expected_dump:
        raise ValueError("descriptor must match custom-tool declaration")


def _default_approval_matrix() -> dict[
    SideEffectClass, tuple[CustomToolApprovalPolicy, ...]
]:
    explicit = (
        CustomToolApprovalPolicy.MILLRACE_EXPLICIT,
        CustomToolApprovalPolicy.OPERATOR_OUT_OF_BAND,
    )
    return {
        SideEffectClass.READ_ONLY: (
            CustomToolApprovalPolicy.NONE,
            CustomToolApprovalPolicy.MILLRACE_EXPLICIT,
            CustomToolApprovalPolicy.OPERATOR_OUT_OF_BAND,
        ),
        SideEffectClass.ARTIFACT_WRITE: explicit,
        SideEffectClass.WORKSPACE_WRITE: explicit,
        SideEffectClass.PROCESS_EXECUTION: explicit,
        SideEffectClass.NETWORK_READ: explicit,
        SideEffectClass.NETWORK_WRITE: explicit,
        SideEffectClass.TERMINAL: explicit,
    }


def _safe_custom_tool_string(value: str, field_name: str, maximum: int) -> str:
    validate_nonblank(value, field_name)
    validate_utf8_size(value, field_name, maximum)
    _reject_secret_material(value, field_name)
    return value


def _validate_utc_timestamp(value: str, field_name: str) -> str:
    validate_nonblank(value, field_name)
    validate_utf8_size(value, field_name, MAX_CUSTOM_TOOL_FIELD_UTF8)
    _reject_secret_material(value, field_name)
    if not value.endswith("Z"):
        raise ValueError(f"{field_name} must be an explicit UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 UTC timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field_name} must be an explicit UTC timestamp")
    return value


def _validate_schema_size(value: Mapping[str, Any], field_name: str) -> None:
    encoded = canonical_json_serialize(value).encode("utf-8")
    if len(encoded) > MAX_CUSTOM_TOOL_SCHEMA_BYTES:
        raise ValueError(f"{field_name} exceeds maximum schema size")


def _reject_secret_material(value: str, field_name: str) -> None:
    if detect_secret_candidate(
        field_path=f"/{field_name}",
        field_name=field_name,
        value=value,
        policy=RedactionPolicy(),
    ):
        raise ValueError(f"{field_name} contains suspected secret material")


def _reject_secret_values(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_secret_values(item, path=f"{path}/{key}")
        return
    if isinstance(value, tuple | list):
        for index, item in enumerate(value):
            _reject_secret_values(item, path=f"{path}/{index}")
        return
    if isinstance(value, str):
        _reject_secret_material(value, path)


def _sorted_string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be an array")
    values = tuple(value)
    if not all(isinstance(item, str) for item in values):
        raise ValueError(f"{field_name} must contain strings")
    return tuple(sorted(values))


def _freeze_json_object(value: Mapping[str, Any]) -> MappingProxyType[str, Any]:
    frozen = _freeze_json_value(dict(value), path="")
    if not isinstance(frozen, MappingProxyType):
        raise ValueError("schema must be a JSON object")
    return frozen


def _freeze_json_value(value: Any, *, path: str) -> Any:
    if isinstance(value, Mapping):
        frozen_items: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda item: str(item[0])):
            if not isinstance(key, str):
                raise ValueError(f"{path or '/'} object keys must be strings")
            frozen_items[key] = _freeze_json_value(item, path=f"{path}/{key}")
        return MappingProxyType(frozen_items)
    if isinstance(value, tuple | list):
        return tuple(
            _freeze_json_value(item, path=f"{path}/{index}")
            for index, item in enumerate(value)
        )
    return _validate_json_scalar(value, path=path)


def _thaw_json_value(value: Any, *, path: str = "") -> Any:
    if isinstance(value, Mapping):
        thawed: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda item: str(item[0])):
            if not isinstance(key, str):
                raise ValueError(f"{path or '/'} object keys must be strings")
            thawed[key] = _thaw_json_value(item, path=f"{path}/{key}")
        return thawed
    if isinstance(value, tuple | list):
        return [
            _thaw_json_value(item, path=f"{path}/{index}")
            for index, item in enumerate(value)
        ]
    return _validate_json_scalar(value, path=path)


def _validate_json_scalar(value: Any, *, path: str) -> str | int | float | bool | None:
    if isinstance(value, str):
        _reject_secret_material(value, path or "value")
        json.dumps(value, allow_nan=False)
        return value
    if isinstance(value, bool):
        json.dumps(value, allow_nan=False)
        return value
    if isinstance(value, int):
        json.dumps(value, allow_nan=False)
        return value
    if isinstance(value, float):
        json.dumps(value, allow_nan=False)
        return value
    if value is None:
        json.dumps(value, allow_nan=False)
        return value
    raise ValueError(f"{path or '/'} must contain only JSON values")


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(
        canonical_json_serialize(_thaw_json_value(payload)).encode("utf-8")
    ).hexdigest()


def _validation_pointer(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "/"
    loc = errors[0].get("loc")
    if not isinstance(loc, tuple | list) or not loc:
        return "/"
    parts = [str(part).replace("~", "~0").replace("/", "~1") for part in loc]
    return "/" + "/".join(parts)


def _validation_missing_field(exc: ValidationError) -> str | None:
    errors = exc.errors()
    if not errors:
        return None
    first = errors[0]
    if first.get("type") != "missing":
        return None
    loc = first.get("loc")
    if not isinstance(loc, tuple | list) or not loc:
        return None
    return str(loc[-1])


def _validation_code(exc: ValidationError) -> CustomToolDiagnosticCode:
    errors = exc.errors()
    if not errors:
        return CustomToolDiagnosticCode.SOURCE_INVALID
    first = errors[0]
    loc = tuple(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "")).lower()
    error_text = str(errors).lower()
    if "secret material" in error_text:
        return CustomToolDiagnosticCode.SECRET_MATERIAL
    if "runtime_kind" in loc:
        return CustomToolDiagnosticCode.RUNTIME_KIND_UNSUPPORTED
    if "produced_artifact_ids" in loc:
        return CustomToolDiagnosticCode.ARTIFACT_POLICY_INVALID
    if (
        "required_capabilities" in loc
        or "require capabilities" in error_text
        or "requires explicit capabilities" in error_text
    ):
        return CustomToolDiagnosticCode.CAPABILITY_MISSING
    if "forbidden approval policy" in error_text:
        return CustomToolDiagnosticCode.FORBIDDEN_TOOL_COMPILED
    if "approval_policy" in loc or "side-effecting custom tools" in error_text:
        return CustomToolDiagnosticCode.APPROVAL_POLICY_INVALID
    if "timeout_policy" in loc:
        return CustomToolDiagnosticCode.TIMEOUT_POLICY_INVALID
    if "output_policy" in loc:
        return CustomToolDiagnosticCode.OUTPUT_POLICY_INVALID
    if "input schema" in message or "input_schema" in loc:
        return CustomToolDiagnosticCode.INPUT_SCHEMA_UNSUPPORTED
    if "output schema" in message or "output_schema" in loc:
        return CustomToolDiagnosticCode.OUTPUT_SCHEMA_UNSUPPORTED
    if "custom tool identities" in error_text:
        return CustomToolDiagnosticCode.DUPLICATE_TOOL
    if "custom tool model_tool_name" in error_text:
        return CustomToolDiagnosticCode.DUPLICATE_MODEL_TOOL_NAME
    if "custom tool implementation_id" in error_text:
        return CustomToolDiagnosticCode.DUPLICATE_IMPLEMENTATION_ID
    return CustomToolDiagnosticCode.SOURCE_INVALID
