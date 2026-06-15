"""Immutable tool descriptor contracts and deterministic registry snapshots."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_serializer,
    field_validator,
    model_validator,
)

from millforge import IdempotencyClass, SideEffectClass, canonical_json_serialize
from millforge.compiler.catalogs import (
    CatalogSnapshotMetadata,
    MAX_CAPABILITY_ID_UTF8,
    MAX_IMPLEMENTATION_ID_UTF8,
    MAX_MODEL_TOOL_NAME_UTF8,
    MAX_TOOL_DESCRIPTION_UTF8,
    RawToolDescriptor,
    ToolCatalogEntry,
    ToolCatalogLookup,
    ToolCatalogSnapshot,
)
from millforge.compiler.diagnostics import detect_secret_candidate
from millforge.compiler.schema_validation import validate_json_schema_subset
from millforge.compiler.validators import (
    validate_artifact_id,
    validate_capability_id,
    validate_canonical_tool_id,
    validate_nonblank,
    validate_tool_version,
    validate_unique,
    validate_utf8_size,
)
from millforge.contracts import RedactionPolicy

DESCRIPTOR_SCHEMA_VERSION = 1
DESCRIPTOR_HASH_KIND = "millforge.tool_descriptor.v1"
SNAPSHOT_KIND = "millforge.tool_registry.snapshot.v1"
SNAPSHOT_ID_KIND = "millforge.tool_registry.snapshot_id.v1"

MAX_TIMEOUT_SECONDS = 86_400
MAX_CANCELLATION_GRACE_SECONDS = 3_600
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_SUMMARY_UTF8 = 65_536

_SIDE_EFFECTS_REQUIRING_CAPABILITY = frozenset(
    item for item in SideEffectClass if item is not SideEffectClass.READ_ONLY
)


JsonValue = Any


class ToolRegistryErrorCode(str, Enum):
    """Stable registry error categories."""

    DESCRIPTOR_INVALID = "descriptor_invalid"
    DUPLICATE_TOOL = "duplicate_tool"
    DUPLICATE_IMPLEMENTATION = "duplicate_implementation"
    REGISTRY_FROZEN = "registry_frozen"
    LOOKUP_INVALID = "lookup_invalid"
    LOOKUP_MISSING = "lookup_missing"
    PROJECTION_INVALID = "projection_invalid"


class ToolRegistryError(ValueError):
    """Typed deterministic registry error with redacted evidence."""

    def __init__(
        self,
        code: ToolRegistryErrorCode,
        message: str,
        *,
        evidence: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.evidence = _redacted_evidence(evidence or {})


class ToolTimeoutPolicy(BaseModel):
    """Descriptor-owned timeout policy data for later execution stages."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: StrictInt = Field(ge=1, le=MAX_TIMEOUT_SECONDS)
    cancellation_grace_seconds: StrictInt = Field(
        ge=1, le=MAX_CANCELLATION_GRACE_SECONDS
    )


class ToolOutputPolicy(BaseModel):
    """Descriptor-owned output policy data for later execution stages."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_output_bytes: StrictInt = Field(ge=1, le=MAX_OUTPUT_BYTES)
    max_summary_utf8: StrictInt = Field(ge=1, le=MAX_OUTPUT_SUMMARY_UTF8)
    redact_secrets: StrictBool


class ToolDescriptor(BaseModel):
    """Immutable registry-owned descriptor with a computed SHA-256 identity."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        hide_input_in_errors=True,
        use_enum_values=False,
    )

    schema_version: Literal[1] = 1
    kind: Literal["millforge.tool_descriptor.v1"] = "millforge.tool_descriptor.v1"
    tool_id: StrictStr
    tool_version: StrictInt = Field(ge=1)
    implementation_id: StrictStr
    model_tool_name: StrictStr
    description: StrictStr
    input_schema: MappingProxyType[str, JsonValue]
    output_schema: MappingProxyType[str, JsonValue]
    required_capabilities: tuple[StrictStr, ...] = Field(default_factory=tuple)
    produced_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    side_effect_class: SideEffectClass
    idempotency: IdempotencyClass
    timeout_policy: ToolTimeoutPolicy
    output_policy: ToolOutputPolicy

    _secret_policy: ClassVar[RedactionPolicy] = RedactionPolicy()

    @field_validator("tool_id")
    @classmethod
    def _tool_id_valid(cls, value: str) -> str:
        return validate_canonical_tool_id(value)

    @field_validator("tool_version")
    @classmethod
    def _tool_version_valid(cls, value: int) -> int:
        return validate_tool_version(value)

    @field_validator("implementation_id")
    @classmethod
    def _implementation_id_valid(cls, value: str) -> str:
        _validate_descriptor_string(
            value, "implementation_id", MAX_IMPLEMENTATION_ID_UTF8
        )
        return value

    @field_validator("model_tool_name")
    @classmethod
    def _model_tool_name_valid(cls, value: str) -> str:
        _validate_descriptor_string(value, "model_tool_name", MAX_MODEL_TOOL_NAME_UTF8)
        return value

    @field_validator("description")
    @classmethod
    def _description_valid(cls, value: str) -> str:
        _validate_descriptor_string(value, "description", MAX_TOOL_DESCRIPTION_UTF8)
        return value

    @field_validator("input_schema", "output_schema", mode="before")
    @classmethod
    def _schema_valid(cls, value: Any, info: Any) -> MappingProxyType[str, Any]:
        _reject_runtime_values(value, path=f"/{info.field_name}")
        _reject_secret_values(value, path=f"/{info.field_name}")
        return validate_json_schema_subset(value, field_name=info.field_name)

    @field_validator("required_capabilities", mode="before")
    @classmethod
    def _capabilities_snapshot(cls, value: Any) -> tuple[str, ...]:
        return _sorted_string_tuple(value, "required_capabilities")

    @field_validator("required_capabilities")
    @classmethod
    def _capabilities_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for capability in value:
            _validate_descriptor_string(
                capability, "required_capabilities", MAX_CAPABILITY_ID_UTF8
            )
            validate_capability_id(capability)
        return validate_unique(value, "required_capabilities")

    @field_validator("produced_artifact_ids", mode="before")
    @classmethod
    def _produced_artifacts_snapshot(cls, value: Any) -> tuple[str, ...]:
        return _sorted_string_tuple(value, "produced_artifact_ids")

    @field_validator("produced_artifact_ids")
    @classmethod
    def _produced_artifacts_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for artifact_id in value:
            validate_artifact_id(artifact_id)
        return validate_unique(value, "produced_artifact_ids")

    @model_validator(mode="after")
    def _descriptor_semantics_valid(self) -> ToolDescriptor:
        if (
            self.side_effect_class in _SIDE_EFFECTS_REQUIRING_CAPABILITY
            or self.produced_artifact_ids
        ) and not self.required_capabilities:
            raise ValueError(
                "side-effecting or artifact-producing descriptors require capabilities"
            )
        return self

    @property
    def descriptor_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json_serialize(descriptor_hash_payload(self)).encode("utf-8")
        ).hexdigest()

    @field_serializer("input_schema", "output_schema")
    def _serialize_schema(self, value: MappingProxyType[str, Any]) -> Any:
        return _thaw_json_value(value)

    def to_raw_descriptor(self) -> RawToolDescriptor:
        """Project into the accepted compiler catalog descriptor contract."""
        return RawToolDescriptor(
            tool_id=self.tool_id,
            tool_version=self.tool_version,
            implementation_id=self.implementation_id,
            descriptor_sha256=self.descriptor_sha256,
            model_tool_name=self.model_tool_name,
            description=self.description,
            input_schema=_thaw_json_value(self.input_schema),
            output_schema=_thaw_json_value(self.output_schema),
            side_effect_class=self.side_effect_class,
            idempotency=self.idempotency,
            required_capabilities=self.required_capabilities,
            produced_artifact_ids=self.produced_artifact_ids,
        )

    def to_catalog_entry(self) -> ToolCatalogEntry:
        """Project into an immutable compiler catalog entry."""
        return ToolCatalogEntry.admit(
            self.to_raw_descriptor(),
            expected_tool_id=self.tool_id,
            expected_tool_version=self.tool_version,
        )


class FrozenDescriptorHashRecord(BaseModel):
    """Immutable descriptor identity record covered by snapshot hashing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_id: StrictStr
    tool_version: StrictInt = Field(ge=1)
    implementation_id: StrictStr
    descriptor_sha256: StrictStr

    @field_validator("tool_id")
    @classmethod
    def _tool_id_valid(cls, value: str) -> str:
        return validate_canonical_tool_id(value)

    @field_validator("tool_version")
    @classmethod
    def _tool_version_valid(cls, value: int) -> int:
        return validate_tool_version(value)


class FrozenToolRegistrySnapshot:
    """Immutable exact-version lookup snapshot for semantic compilation."""

    def __init__(self, descriptors: tuple[ToolDescriptor, ...]) -> None:
        entries: dict[tuple[str, int], ToolCatalogEntry] = {}
        records: list[FrozenDescriptorHashRecord] = []
        for descriptor in descriptors:
            try:
                entry = descriptor.to_catalog_entry()
            except Exception as exc:
                raise ToolRegistryError(
                    ToolRegistryErrorCode.PROJECTION_INVALID,
                    "descriptor projection failed",
                    evidence={"error_type": type(exc).__name__},
                ) from exc
            entries[(descriptor.tool_id, descriptor.tool_version)] = entry
            records.append(
                FrozenDescriptorHashRecord(
                    tool_id=descriptor.tool_id,
                    tool_version=descriptor.tool_version,
                    implementation_id=entry.implementation_id,
                    descriptor_sha256=entry.descriptor_sha256,
                )
            )
        self._entries = MappingProxyType(dict(sorted(entries.items())))
        self._descriptor_hash_records = tuple(
            sorted(records, key=lambda record: (record.tool_id, record.tool_version))
        )
        snapshot_payload = {
            "schema_version": 1,
            "kind": SNAPSHOT_KIND,
            "descriptors": [
                record.model_dump(mode="json")
                for record in self._descriptor_hash_records
            ],
        }
        snapshot_sha256 = hashlib.sha256(
            canonical_json_serialize(snapshot_payload).encode("utf-8")
        ).hexdigest()
        snapshot_id_payload = {
            "schema_version": 1,
            "kind": SNAPSHOT_ID_KIND,
            "snapshot_sha256": snapshot_sha256,
        }
        snapshot_id = hashlib.sha256(
            canonical_json_serialize(snapshot_id_payload).encode("utf-8")
        ).hexdigest()
        self._snapshot_sha256 = snapshot_sha256
        self._snapshot_id = snapshot_id
        CatalogSnapshotMetadata(
            snapshot_id=self._snapshot_id,
            snapshot_sha256=self._snapshot_sha256,
        )

    @property
    def snapshot_id(self) -> str:
        return self._snapshot_id

    @property
    def snapshot_sha256(self) -> str:
        return self._snapshot_sha256

    @property
    def descriptor_hash_records(self) -> tuple[FrozenDescriptorHashRecord, ...]:
        return self._descriptor_hash_records

    def resolve_exact(self, tool_id: str, tool_version: int) -> ToolCatalogLookup:
        try:
            if _looks_like_alias(tool_id):
                raise ValueError("tool_id aliases are not supported")
            if not isinstance(tool_version, int) or isinstance(tool_version, bool):
                raise ValueError("tool_version must be an integer")
            valid_tool_id = validate_canonical_tool_id(tool_id)
            valid_tool_version = validate_tool_version(tool_version)
        except Exception:
            return ToolCatalogLookup.invalid(
                error_code=ToolRegistryErrorCode.LOOKUP_INVALID.value,
                evidence={
                    "tool_id": _safe_evidence_text(tool_id),
                    "tool_version": _safe_evidence_text(tool_version),
                },
            )
        entry = self._entries.get((valid_tool_id, valid_tool_version))
        if entry is None:
            return ToolCatalogLookup.missing(
                error_code=ToolRegistryErrorCode.LOOKUP_MISSING.value,
                evidence={"tool_id": valid_tool_id, "tool_version": str(tool_version)},
            )
        return ToolCatalogLookup.found(entry)


class ToolRegistry:
    """Explicit in-process registry for immutable descriptors."""

    def __init__(self) -> None:
        self._descriptors: dict[tuple[str, int], ToolDescriptor] = {}
        self._implementation_ids: set[str] = set()
        self._snapshot: FrozenToolRegistrySnapshot | None = None
        self._frozen = False

    def register(self, descriptor: ToolDescriptor) -> None:
        if self._frozen:
            raise ToolRegistryError(
                ToolRegistryErrorCode.REGISTRY_FROZEN,
                "registry is frozen",
            )
        if not isinstance(descriptor, ToolDescriptor):
            raise ToolRegistryError(
                ToolRegistryErrorCode.DESCRIPTOR_INVALID,
                "descriptor must be a ToolDescriptor",
            )
        key = (descriptor.tool_id, descriptor.tool_version)
        if key in self._descriptors:
            raise ToolRegistryError(
                ToolRegistryErrorCode.DUPLICATE_TOOL,
                "duplicate tool descriptor",
                evidence={"tool_id": descriptor.tool_id},
            )
        if descriptor.implementation_id in self._implementation_ids:
            raise ToolRegistryError(
                ToolRegistryErrorCode.DUPLICATE_IMPLEMENTATION,
                "duplicate implementation id",
                evidence={"implementation_id": descriptor.implementation_id},
            )
        try:
            descriptor.to_catalog_entry()
        except Exception as exc:
            raise ToolRegistryError(
                ToolRegistryErrorCode.PROJECTION_INVALID,
                "descriptor projection failed",
                evidence={"error_type": type(exc).__name__},
            ) from exc
        self._descriptors[key] = descriptor
        self._implementation_ids.add(descriptor.implementation_id)

    def freeze(self) -> FrozenToolRegistrySnapshot:
        if self._snapshot is None:
            descriptors = tuple(
                descriptor
                for _, descriptor in sorted(
                    self._descriptors.items(), key=lambda item: item[0]
                )
            )
            self._snapshot = FrozenToolRegistrySnapshot(descriptors)
            self._frozen = True
        return self._snapshot


def descriptor_hash_payload(descriptor: ToolDescriptor) -> dict[str, Any]:
    """Return the deterministic descriptor payload covered by SHA-256."""
    return {
        "schema_version": descriptor.schema_version,
        "kind": descriptor.kind,
        "tool_id": descriptor.tool_id,
        "tool_version": descriptor.tool_version,
        "implementation_id": descriptor.implementation_id,
        "model_tool_name": descriptor.model_tool_name,
        "description": descriptor.description,
        "input_schema": _thaw_json_value(descriptor.input_schema),
        "output_schema": _thaw_json_value(descriptor.output_schema),
        "required_capabilities": list(descriptor.required_capabilities),
        "produced_artifact_ids": list(descriptor.produced_artifact_ids),
        "side_effect_class": descriptor.side_effect_class.value,
        "idempotency": descriptor.idempotency.value,
        "timeout_policy": descriptor.timeout_policy.model_dump(mode="json"),
        "output_policy": descriptor.output_policy.model_dump(mode="json"),
    }


def _sorted_string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be an array")
    return tuple(sorted(value))


def _validate_descriptor_string(value: str, field_name: str, maximum: int) -> None:
    validate_nonblank(value, field_name)
    validate_utf8_size(value, field_name, maximum)
    if detect_secret_candidate(
        field_path=f"/{field_name}",
        field_name=field_name,
        value=value,
        policy=RedactionPolicy(),
    ):
        raise ValueError(f"{field_name} contains suspected secret material")


def _reject_runtime_values(value: Any, *, path: str) -> None:
    if callable(value):
        raise ValueError(f"{path} must not contain runtime values")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_runtime_values(item, path=f"{path}/{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_runtime_values(item, path=f"{path}/{index}")


def _reject_secret_values(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_secret_values(item, path=f"{path}/{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secret_values(item, path=f"{path}/{index}")
        return
    if isinstance(value, str) and detect_secret_candidate(
        field_path="/descriptor_schema_value",
        field_name="descriptor_schema_value",
        value=value,
        policy=RedactionPolicy(),
    ):
        raise ValueError("schema contains suspected secret material")


def _looks_like_alias(tool_id: Any) -> bool:
    return isinstance(tool_id, str) and (
        tool_id == "latest" or tool_id.endswith(".latest")
    )


def _thaw_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def _redacted_evidence(values: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted((key, _safe_evidence_text(value)) for key, value in values.items())
    )


def _safe_evidence_text(value: Any) -> str:
    text = str(value)
    policy = RedactionPolicy()
    if detect_secret_candidate(
        field_path="/evidence",
        field_name="evidence",
        value=text,
        policy=policy,
    ):
        return policy.replacement
    return validate_utf8_size(text, "evidence", 512)


assert isinstance(FrozenToolRegistrySnapshot(()), ToolCatalogSnapshot)
