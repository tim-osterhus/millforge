"""Deterministic catalog snapshot contracts and entry admission."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum
from inspect import getattr_static
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_serializer,
    field_validator,
    model_validator,
)

from millforge import CompiledModelProfile, IdempotencyClass, SideEffectClass
from millforge.compiler.diagnostics import detect_secret_candidate
from millforge.compiler.schema_validation import validate_json_schema_subset
from millforge.compiler.validators import (
    TOOL_VERSION_MAX,
    validate_artifact_id,
    validate_capability_id,
    validate_canonical_tool_id,
    validate_nonblank,
    validate_profile_id,
    validate_sha256,
    validate_unique,
    validate_utf8_size,
)
from millforge.contracts import RedactionPolicy

MAX_TOOL_DESCRIPTION_UTF8 = 4096
MAX_IMPLEMENTATION_ID_UTF8 = 256
MAX_MODEL_TOOL_NAME_UTF8 = 64
MAX_CAPABILITY_ID_UTF8 = 160
MF_R009 = "MF-R009"
DISCOVERY_NOT_CATALOG_CODE = "MF-C001_DISCOVERY_NOT_CATALOG"


class CatalogLookupClassification(str, Enum):
    """Closed catalog lookup classifications."""

    FOUND = "found"
    MISSING = "missing"
    INVALID = "invalid"


class CatalogMetadataError(ValueError):
    """Fail-closed snapshot metadata error for semantic resolution."""

    diagnostic_code = MF_R009

    def __init__(self, message: str, *, evidence: Mapping[str, str] | None = None):
        super().__init__(message)
        self.evidence = _redacted_evidence(evidence or {})


class CatalogSnapshotMetadata(BaseModel):
    """Validated immutable snapshot identity captured once per invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: StrictStr
    snapshot_sha256: StrictStr

    @field_validator("snapshot_id", "snapshot_sha256")
    @classmethod
    def _snapshot_hash_valid(cls, value: str, info: Any) -> str:
        return validate_sha256(value, info.field_name)


@runtime_checkable
class ToolCatalogSnapshot(Protocol):
    """Synchronous exact-version tool catalog snapshot."""

    @property
    def snapshot_id(self) -> str: ...

    @property
    def snapshot_sha256(self) -> str: ...

    def resolve_exact(self, tool_id: str, tool_version: int) -> ToolCatalogLookup: ...


@runtime_checkable
class ModelProfileCatalogSnapshot(Protocol):
    """Synchronous exact-profile model catalog snapshot."""

    @property
    def snapshot_id(self) -> str: ...

    @property
    def snapshot_sha256(self) -> str: ...

    def resolve_exact(self, profile_id: str) -> ModelProfileCatalogLookup: ...


class RawToolDescriptor(BaseModel):
    """Closed raw descriptor accepted from a catalog snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: StrictStr
    tool_version: StrictInt = Field(ge=1, le=TOOL_VERSION_MAX)
    implementation_id: StrictStr
    descriptor_sha256: StrictStr
    model_tool_name: StrictStr
    description: StrictStr
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    side_effect_class: SideEffectClass
    idempotency: IdempotencyClass
    required_capabilities: tuple[StrictStr, ...] = Field(default_factory=tuple)
    produced_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)

    @field_validator("tool_id")
    @classmethod
    def _tool_id_valid(cls, value: str) -> str:
        return validate_canonical_tool_id(value)

    @field_validator("implementation_id")
    @classmethod
    def _implementation_id_valid(cls, value: str) -> str:
        validate_nonblank(value, "implementation_id")
        return validate_utf8_size(
            value, "implementation_id", MAX_IMPLEMENTATION_ID_UTF8
        )

    @field_validator("descriptor_sha256")
    @classmethod
    def _descriptor_hash_valid(cls, value: str) -> str:
        return validate_sha256(value, "descriptor_sha256")

    @field_validator("model_tool_name")
    @classmethod
    def _model_tool_name_valid(cls, value: str) -> str:
        validate_nonblank(value, "model_tool_name")
        return validate_utf8_size(value, "model_tool_name", MAX_MODEL_TOOL_NAME_UTF8)

    @field_validator("description")
    @classmethod
    def _description_valid(cls, value: str) -> str:
        validate_nonblank(value, "description")
        return validate_utf8_size(value, "description", MAX_TOOL_DESCRIPTION_UTF8)

    @field_validator("required_capabilities")
    @classmethod
    def _capabilities_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for capability in value:
            validate_capability_id(capability)
        return validate_unique(value, "required_capabilities")

    @field_validator("produced_artifact_ids")
    @classmethod
    def _produced_artifacts_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for artifact_id in value:
            validate_artifact_id(artifact_id)
        return validate_unique(value, "produced_artifact_ids")

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _schema_subset_valid(
        cls, value: Mapping[str, Any], info: Any
    ) -> Mapping[str, Any]:
        _reject_runtime_values(value, path=f"/{info.field_name}")
        validate_json_schema_subset(value, field_name=info.field_name)
        return value


class ToolCatalogEntry(BaseModel):
    """Deeply immutable compiler-facing admitted tool descriptor."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    tool_id: StrictStr
    tool_version: StrictInt = Field(ge=1, le=TOOL_VERSION_MAX)
    implementation_id: StrictStr
    descriptor_sha256: StrictStr
    model_tool_name: StrictStr
    description: StrictStr
    input_schema: MappingProxyType[str, Any]
    output_schema: MappingProxyType[str, Any]
    side_effect_class: SideEffectClass
    idempotency: IdempotencyClass
    required_capabilities: tuple[StrictStr, ...] = Field(default_factory=tuple)
    produced_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    timeout_policy: Any | None = None
    output_policy: Any | None = None

    @field_serializer("input_schema", "output_schema")
    def _serialize_schema(self, value: MappingProxyType[str, Any]) -> Any:
        return _thaw_json_value(value)

    @classmethod
    def admit(
        cls,
        descriptor: RawToolDescriptor | Mapping[str, Any],
        *,
        expected_tool_id: str,
        expected_tool_version: int,
        timeout_policy: Any | None = None,
        output_policy: Any | None = None,
    ) -> ToolCatalogEntry:
        """Admit a raw descriptor into the immutable semantic boundary."""
        expected_tool_id = validate_canonical_tool_id(expected_tool_id)
        if expected_tool_version < 1 or expected_tool_version > TOOL_VERSION_MAX:
            raise ValueError("expected_tool_version must be in range 1..2147483647")

        raw = (
            descriptor
            if isinstance(descriptor, RawToolDescriptor)
            else RawToolDescriptor.model_validate(descriptor)
        )
        if raw.tool_id != expected_tool_id:
            raise ValueError("descriptor tool_id does not match requested tool_id")
        if raw.tool_version != expected_tool_version:
            raise ValueError(
                "descriptor tool_version does not match requested tool_version"
            )
        _reject_runtime_values(raw.input_schema, path="/input_schema")
        _reject_runtime_values(raw.output_schema, path="/output_schema")
        return cls(
            tool_id=raw.tool_id,
            tool_version=raw.tool_version,
            implementation_id=raw.implementation_id,
            descriptor_sha256=raw.descriptor_sha256,
            model_tool_name=raw.model_tool_name,
            description=raw.description,
            input_schema=validate_json_schema_subset(
                raw.input_schema, field_name="input_schema"
            ),
            output_schema=validate_json_schema_subset(
                raw.output_schema, field_name="output_schema"
            ),
            side_effect_class=raw.side_effect_class,
            idempotency=raw.idempotency,
            required_capabilities=raw.required_capabilities,
            produced_artifact_ids=raw.produced_artifact_ids,
            timeout_policy=timeout_policy,
            output_policy=output_policy,
        )


class ToolCatalogLookup(BaseModel):
    """Closed exact-version tool lookup outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    classification: CatalogLookupClassification
    entry: ToolCatalogEntry | None = None
    error_code: StrictStr | None = None
    evidence: tuple[tuple[StrictStr, StrictStr], ...] = Field(default_factory=tuple)

    @field_validator("error_code")
    @classmethod
    def _error_code_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        validate_nonblank(value, "error_code")
        return validate_utf8_size(value, "error_code", 128)

    @model_validator(mode="after")
    def _classification_shape_valid(self) -> ToolCatalogLookup:
        if self.classification is CatalogLookupClassification.FOUND:
            if self.entry is None:
                raise ValueError("found tool lookups require an entry")
            if self.error_code is not None or self.evidence:
                raise ValueError("found tool lookups cannot carry errors")
        elif self.entry is not None:
            raise ValueError("missing or invalid tool lookups cannot carry an entry")
        return self

    @classmethod
    def found(cls, entry: ToolCatalogEntry) -> ToolCatalogLookup:
        return cls(classification=CatalogLookupClassification.FOUND, entry=entry)

    @classmethod
    def missing(
        cls, *, error_code: str | None = None, evidence: Mapping[str, str] | None = None
    ) -> ToolCatalogLookup:
        return cls(
            classification=CatalogLookupClassification.MISSING,
            error_code=error_code,
            evidence=_redacted_evidence(evidence or {}),
        )

    @classmethod
    def invalid(
        cls, *, error_code: str | None = None, evidence: Mapping[str, str] | None = None
    ) -> ToolCatalogLookup:
        return cls(
            classification=CatalogLookupClassification.INVALID,
            error_code=error_code,
            evidence=_redacted_evidence(evidence or {}),
        )


class ModelProfileCatalogLookup(BaseModel):
    """Closed model-profile lookup outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    classification: CatalogLookupClassification
    profile: CompiledModelProfile | None = None
    error_code: StrictStr | None = None
    evidence: tuple[tuple[StrictStr, StrictStr], ...] = Field(default_factory=tuple)

    @field_validator("error_code")
    @classmethod
    def _error_code_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        validate_nonblank(value, "error_code")
        return validate_utf8_size(value, "error_code", 128)

    @model_validator(mode="after")
    def _classification_shape_valid(self) -> ModelProfileCatalogLookup:
        if self.classification is CatalogLookupClassification.FOUND:
            if self.profile is None:
                raise ValueError("found model-profile lookups require a profile")
            if self.error_code is not None or self.evidence:
                raise ValueError("found model-profile lookups cannot carry errors")
        elif self.profile is not None:
            raise ValueError(
                "missing or invalid model-profile lookups cannot carry a profile"
            )
        return self

    @classmethod
    def found(cls, profile: CompiledModelProfile) -> ModelProfileCatalogLookup:
        return cls(classification=CatalogLookupClassification.FOUND, profile=profile)

    @classmethod
    def missing(
        cls, *, error_code: str | None = None, evidence: Mapping[str, str] | None = None
    ) -> ModelProfileCatalogLookup:
        return cls(
            classification=CatalogLookupClassification.MISSING,
            error_code=error_code,
            evidence=_redacted_evidence(evidence or {}),
        )

    @classmethod
    def invalid(
        cls, *, error_code: str | None = None, evidence: Mapping[str, str] | None = None
    ) -> ModelProfileCatalogLookup:
        return cls(
            classification=CatalogLookupClassification.INVALID,
            error_code=error_code,
            evidence=_redacted_evidence(evidence or {}),
        )


def capture_catalog_snapshot_metadata(
    snapshot: ToolCatalogSnapshot | ModelProfileCatalogSnapshot,
) -> CatalogSnapshotMetadata:
    """Read and validate snapshot metadata exactly once."""
    try:
        snapshot_id = snapshot.snapshot_id
        snapshot_sha256 = snapshot.snapshot_sha256
    except Exception as exc:
        raise CatalogMetadataError(
            "catalog snapshot metadata could not be read",
            evidence={"error_type": type(exc).__name__},
        ) from exc
    try:
        return CatalogSnapshotMetadata(
            snapshot_id=snapshot_id,
            snapshot_sha256=snapshot_sha256,
        )
    except ValueError as exc:
        raise CatalogMetadataError(
            "catalog snapshot metadata is invalid",
            evidence={"error_type": type(exc).__name__},
        ) from exc


def is_connector_discovery_snapshot_like(snapshot: object) -> bool:
    """Return whether an object is discovery-shaped, not a semantic catalog."""
    return all(
        _has_static_attribute(snapshot, attribute)
        for attribute in (
            "connector_identity",
            "provider_tools",
            "discovery_snapshot_sha256",
        )
    )


def admit_model_profile(
    raw_profile: CompiledModelProfile | Mapping[str, Any],
    *,
    expected_profile_id: str,
) -> CompiledModelProfile:
    """Admit only the accepted provider-neutral compiled profile contract."""
    expected_profile_id = validate_profile_id(expected_profile_id)
    if isinstance(raw_profile, CompiledModelProfile):
        profile = raw_profile.model_copy(deep=True)
    elif isinstance(raw_profile, Mapping):
        _reject_forbidden_model_profile_material(raw_profile)
        profile = CompiledModelProfile.model_validate(raw_profile)
    else:
        raise ValueError("model profile must be a CompiledModelProfile")
    if profile.profile_id != expected_profile_id:
        raise ValueError("model profile id does not match requested profile_id")
    return profile


def _has_static_attribute(value: object, attribute: str) -> bool:
    try:
        getattr_static(value, attribute)
    except AttributeError:
        return False
    return True


def _freeze_json_object(value: Mapping[str, Any]) -> MappingProxyType[str, Any]:
    frozen = _freeze_json_value(dict(value), path="")
    if not isinstance(frozen, MappingProxyType):
        raise ValueError("schema must be a JSON object")
    return frozen


def _freeze_json_value(value: Any, *, path: str) -> Any:
    if isinstance(value, Mapping):
        frozen_items: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path or '/'} object keys must be strings")
            frozen_items[key] = _freeze_json_value(item, path=f"{path}/{key}")
        return MappingProxyType(frozen_items)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json_value(item, path=f"{path}/{index}")
            for index, item in enumerate(value)
        )
    if isinstance(value, (str, int, float, bool)) or value is None:
        json.dumps(value, allow_nan=False)
        return value
    raise ValueError(f"{path or '/'} must contain only JSON values")


def _thaw_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def _reject_runtime_values(value: Any, *, path: str) -> None:
    if callable(value):
        raise ValueError(f"{path} must not contain callables")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_runtime_values(item, path=f"{path}/{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_runtime_values(item, path=f"{path}/{index}")


def _reject_forbidden_model_profile_material(value: Mapping[str, Any]) -> None:
    policy = RedactionPolicy()
    for key, item in value.items():
        if key != "profile_id":
            raise ValueError("model profile contains runtime-only fields")
        if isinstance(item, str) and detect_secret_candidate(
            field_path=f"/{key}",
            field_name=key,
            value=item,
            policy=policy,
        ):
            raise ValueError("model profile contains secret material")


def _redacted_evidence(values: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    policy = RedactionPolicy()
    redacted: list[tuple[str, str]] = []
    for key in sorted(values):
        value = values[key]
        redacted_value = (
            policy.replacement
            if detect_secret_candidate(
                field_path=f"/{key}",
                field_name=key,
                value=value,
                policy=policy,
            )
            else value
        )
        redacted.append((key, validate_utf8_size(redacted_value, key, 512)))
    return tuple(redacted)
