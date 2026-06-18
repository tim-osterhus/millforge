"""Frozen connector identity, discovery, admission, and diagnostic contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
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
    validate_capability_id,
    validate_canonical_tool_id,
    validate_nonblank,
    validate_sha256,
    validate_tool_version,
    validate_unique,
    validate_utf8_size,
)
from millforge.connectors.diagnostics import (
    ConnectorDiagnostic,
    ConnectorDiagnosticCode,
    ConnectorDiagnosticPhase,
    malformed_input_diagnostic,
)
from millforge.contracts import RedactionPolicy
from millforge.tools.registry import ToolDescriptor, ToolOutputPolicy, ToolTimeoutPolicy

CONNECTOR_IDENTITY_HASH_KIND = "millforge.connector.identity.v1"
CONNECTOR_DISCOVERY_TOOL_HASH_KIND = "millforge.connector.discovery_tool.v1"
CONNECTOR_DISCOVERY_SNAPSHOT_HASH_KIND = "millforge.connector.discovery_snapshot.v1"
CONNECTOR_ADMISSION_RECORD_HASH_KIND = "millforge.connector.admission_record.v1"
CONNECTOR_DISCOVERY_SNAPSHOT_SCHEMA = "millforge.connector.discovery_snapshot"
CONNECTOR_DISCOVERY_SNAPSHOT_KIND = "connector_discovery_snapshot"
CONNECTOR_DISCOVERY_SNAPSHOT_VERSION = "1.0"
CONNECTOR_ADMISSION_MANIFEST_SCHEMA = "millforge.connector.admission_manifest"
CONNECTOR_ADMISSION_MANIFEST_KIND = "connector_admission_manifest"
CONNECTOR_ADMISSION_MANIFEST_VERSION = "1.0"

MAX_CONNECTOR_ID_UTF8 = 160
MAX_CONNECTOR_FIELD_UTF8 = 512
MAX_PROVIDER_TOOL_NAME_UTF8 = 160
MAX_PROVIDER_DESCRIPTION_UTF8 = 16_384
MAX_ADMITTED_DESCRIPTION_UTF8 = 65_536
_SECRET_REF_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SIDE_EFFECTS_REQUIRING_APPROVAL = frozenset(
    item for item in SideEffectClass if item is not SideEffectClass.READ_ONLY
)

JsonValue = Any


class ConnectorTransportKind(str, Enum):
    """Closed connector transport identity values."""

    STDIO = "stdio"
    HTTP = "http"


class ConnectorProtocol(str, Enum):
    """Closed connector protocol identity values."""

    MCP = "mcp"


class DescriptionPolicy(str, Enum):
    """Closed description provenance policy values."""

    OPERATOR_SUPPLIED = "operator_supplied"
    PROVIDER_SANITIZED = "provider_sanitized"
    PROVIDER_REJECTED = "provider_rejected"


class InputSchemaPolicy(str, Enum):
    """Closed input schema admission policy values."""

    PROVIDER_EXACT = "provider_exact"
    OPERATOR_OVERLAY = "operator_overlay"


class OutputSchemaPolicy(str, Enum):
    """Closed output schema admission policy values."""

    PROVIDER_EXACT = "provider_exact"
    OPERATOR_SUPPLIED = "operator_supplied"


class ConnectorApprovalPolicy(str, Enum):
    """Closed approval policy values for admitted connector tools."""

    NONE = "none"
    MILLRACE_EXPLICIT = "millrace_explicit"
    OPERATOR_OUT_OF_BAND = "operator_out_of_band"
    FORBIDDEN = "forbidden"


class ConnectorIdentity(BaseModel):
    """Immutable connector implementation identity."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        hide_input_in_errors=True,
        use_enum_values=False,
    )

    connector_id: StrictStr
    protocol: ConnectorProtocol
    protocol_version: StrictStr
    transport_kind: ConnectorTransportKind
    implementation_name: StrictStr
    implementation_version: StrictStr | None = None
    implementation_digest: StrictStr | None = None
    server_reported_name: StrictStr | None = None
    server_reported_version: StrictStr | None = None
    configured_secret_refs: tuple[StrictStr, ...] = Field(default_factory=tuple)
    discovered_at: StrictStr

    @field_validator(
        "connector_id",
        "protocol_version",
        "implementation_name",
        "implementation_version",
        "implementation_digest",
        "server_reported_name",
        "server_reported_version",
    )
    @classmethod
    def _identity_text_valid(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        maximum = (
            MAX_CONNECTOR_ID_UTF8
            if info.field_name == "connector_id"
            else MAX_CONNECTOR_FIELD_UTF8
        )
        return _safe_connector_string(value, info.field_name, maximum)

    @field_validator("implementation_digest")
    @classmethod
    def _implementation_digest_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_sha256(value, "implementation_digest")

    @field_validator("discovered_at")
    @classmethod
    def _discovered_at_valid(cls, value: str) -> str:
        return _validate_utc_timestamp(value, "discovered_at")

    @field_validator("configured_secret_refs", mode="before")
    @classmethod
    def _secret_refs_snapshot(cls, value: Any) -> tuple[str, ...]:
        values = _sorted_string_tuple(value, "configured_secret_refs")
        for item in values:
            if _SECRET_REF_RE.fullmatch(item) is None:
                raise ValueError(
                    "configured_secret_refs must contain secret reference names"
                )
        return validate_unique(values, "configured_secret_refs")

    @model_validator(mode="after")
    def _identity_complete(self) -> ConnectorIdentity:
        if self.implementation_version is None and self.implementation_digest is None:
            raise ValueError(
                "connector identity requires implementation_version or implementation_digest"
            )
        return self

    @property
    def identity_sha256(self) -> str:
        return _sha256_hex(
            {
                "kind": CONNECTOR_IDENTITY_HASH_KIND,
                "connector_id": self.connector_id,
                "protocol": self.protocol.value,
                "protocol_version": self.protocol_version,
                "transport_kind": self.transport_kind.value,
                "implementation_name": self.implementation_name,
                "implementation_version": self.implementation_version,
                "implementation_digest": self.implementation_digest,
                "server_reported_name": self.server_reported_name,
                "server_reported_version": self.server_reported_version,
                "configured_secret_refs": list(self.configured_secret_refs),
            }
        )


class ExpectedConnectorIdentity(BaseModel):
    """Exact expected identity constraints required before admission."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        hide_input_in_errors=True,
    )

    protocol: ConnectorProtocol
    protocol_version: StrictStr
    transport_kind: ConnectorTransportKind
    implementation_name: StrictStr
    implementation_version: StrictStr | None = None
    implementation_digest: StrictStr | None = None

    @field_validator(
        "protocol_version",
        "implementation_name",
        "implementation_version",
        "implementation_digest",
    )
    @classmethod
    def _expected_text_valid(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _safe_connector_string(value, info.field_name, MAX_CONNECTOR_FIELD_UTF8)

    @field_validator("implementation_digest")
    @classmethod
    def _expected_digest_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_sha256(value, "implementation_digest")

    @model_validator(mode="after")
    def _expected_complete(self) -> ExpectedConnectorIdentity:
        if self.implementation_version is None and self.implementation_digest is None:
            raise ValueError(
                "expected identity requires implementation_version or implementation_digest"
            )
        return self

    def matches(self, identity: ConnectorIdentity) -> bool:
        """Return whether the supplied identity exactly satisfies this expectation."""
        return (
            self.protocol == identity.protocol
            and self.protocol_version == identity.protocol_version
            and self.transport_kind == identity.transport_kind
            and self.implementation_name == identity.implementation_name
            and self.implementation_version == identity.implementation_version
            and self.implementation_digest == identity.implementation_digest
        )


class DiscoveredProviderTool(BaseModel):
    """Untrusted provider-discovered tool payload preserved for offline admission."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        hide_input_in_errors=True,
    )

    provider_tool_name: StrictStr
    provider_description: StrictStr = ""
    input_schema: Mapping[str, JsonValue]
    output_schema: Mapping[str, JsonValue] | None = None
    provider_annotations: Mapping[str, JsonValue] = Field(
        default_factory=lambda: MappingProxyType({})
    )

    @field_validator("provider_tool_name")
    @classmethod
    def _provider_tool_name_valid(cls, value: str) -> str:
        return _safe_connector_string(
            value, "provider_tool_name", MAX_PROVIDER_TOOL_NAME_UTF8
        )

    @field_validator("provider_description")
    @classmethod
    def _provider_description_valid(cls, value: str) -> str:
        validate_utf8_size(value, "provider_description", MAX_PROVIDER_DESCRIPTION_UTF8)
        return value

    @field_validator("input_schema", "output_schema", "provider_annotations")
    @classmethod
    def _raw_mapping_frozen(
        cls, value: Any, info: Any
    ) -> MappingProxyType[str, Any] | None:
        if value is None and info.field_name == "output_schema":
            return None
        if not isinstance(value, Mapping):
            raise ValueError(f"{info.field_name} must be a JSON object")
        return _freeze_json_object(value)

    @property
    def input_schema_sha256(self) -> str:
        return _sha256_hex(_thaw_json_value(self.input_schema))

    @property
    def output_schema_sha256(self) -> str | None:
        if self.output_schema is None:
            return None
        return _sha256_hex(_thaw_json_value(self.output_schema))

    @property
    def provider_description_sha256(self) -> str:
        return _sha256_hex(self.provider_description)

    @property
    def raw_tool_sha256(self) -> str:
        return _sha256_hex(
            {
                "kind": CONNECTOR_DISCOVERY_TOOL_HASH_KIND,
                "provider_tool_name": self.provider_tool_name,
                "provider_description": self.provider_description,
                "input_schema": _thaw_json_value(self.input_schema),
                "output_schema": _thaw_json_value(self.output_schema),
                "provider_annotations": _thaw_json_value(self.provider_annotations),
            }
        )

    @field_serializer("input_schema", "output_schema", "provider_annotations")
    def _serialize_mapping(self, value: Any) -> Any:
        return _thaw_json_value(value)


class ConnectorDiscoverySnapshot(BaseModel):
    """Immutable non-authoritative connector discovery snapshot."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        hide_input_in_errors=True,
    )

    schema_version: StrictStr = CONNECTOR_DISCOVERY_SNAPSHOT_SCHEMA
    kind: StrictStr = CONNECTOR_DISCOVERY_SNAPSHOT_KIND
    version: StrictStr = CONNECTOR_DISCOVERY_SNAPSHOT_VERSION
    connector_identity: ConnectorIdentity
    provider_tools: tuple[DiscoveredProviderTool, ...]
    created_at: StrictStr
    provider_metadata: Mapping[str, JsonValue] = Field(
        default_factory=lambda: MappingProxyType({})
    )

    @field_validator("schema_version")
    @classmethod
    def _schema_version_valid(cls, value: str) -> str:
        if value != CONNECTOR_DISCOVERY_SNAPSHOT_SCHEMA:
            raise ValueError(
                "schema_version must identify connector discovery snapshots"
            )
        return value

    @field_validator("kind")
    @classmethod
    def _kind_valid(cls, value: str) -> str:
        if value != CONNECTOR_DISCOVERY_SNAPSHOT_KIND:
            raise ValueError("kind must be connector_discovery_snapshot")
        return value

    @field_validator("version")
    @classmethod
    def _version_valid(cls, value: str) -> str:
        if value != CONNECTOR_DISCOVERY_SNAPSHOT_VERSION:
            raise ValueError("version must be 1.0")
        return value

    @field_validator("created_at")
    @classmethod
    def _created_at_valid(cls, value: str) -> str:
        return _validate_utc_timestamp(value, "created_at")

    @field_validator("provider_tools", mode="before")
    @classmethod
    def _provider_tools_tuple(cls, value: Any) -> tuple[DiscoveredProviderTool, ...]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("provider_tools must be an array")
        return tuple(value)

    @field_validator("provider_metadata")
    @classmethod
    def _metadata_frozen(cls, value: Any) -> MappingProxyType[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("provider_metadata must be a JSON object")
        return _freeze_json_object(value)

    @property
    def duplicate_provider_names(self) -> tuple[str, ...]:
        counts: dict[str, int] = {}
        for tool in self.provider_tools:
            counts[tool.provider_tool_name] = counts.get(tool.provider_tool_name, 0) + 1
        return tuple(name for name, count in sorted(counts.items()) if count > 1)

    @property
    def discovery_snapshot_sha256(self) -> str:
        return _sha256_hex(
            {
                "kind": CONNECTOR_DISCOVERY_SNAPSHOT_HASH_KIND,
                "schema_version": self.schema_version,
                "snapshot_kind": self.kind,
                "version": self.version,
                "connector_identity_sha256": self.connector_identity.identity_sha256,
                "created_at": self.created_at,
                "provider_tools": [
                    {
                        "provider_tool_name": tool.provider_tool_name,
                        "raw_tool_sha256": tool.raw_tool_sha256,
                    }
                    for tool in sorted(
                        self.provider_tools, key=lambda item: item.provider_tool_name
                    )
                ],
                "provider_metadata": _thaw_json_value(self.provider_metadata),
            }
        )

    @field_serializer("provider_metadata")
    def _serialize_metadata(self, value: Any) -> Any:
        return _thaw_json_value(value)


class ConnectorToolSelection(BaseModel):
    """Operator-selected discovered tool and admitted descriptor shape."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        hide_input_in_errors=True,
    )

    provider_tool_name: StrictStr
    tool_id: StrictStr
    tool_version: StrictInt = Field(ge=1)
    implementation_id: StrictStr
    model_tool_name: StrictStr
    description: StrictStr
    description_policy: DescriptionPolicy
    input_schema_policy: InputSchemaPolicy
    output_schema_policy: OutputSchemaPolicy
    input_schema: Mapping[str, JsonValue] | None = None
    output_schema: Mapping[str, JsonValue] | None = None
    required_capabilities: tuple[StrictStr, ...]
    produced_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    side_effect_class: SideEffectClass
    idempotency: IdempotencyClass
    timeout_policy: ToolTimeoutPolicy
    output_policy: ToolOutputPolicy
    approval_policy: ConnectorApprovalPolicy
    expected_raw_tool_sha256: StrictStr | None = None
    expected_descriptor_sha256: StrictStr | None = None
    expected_admission_record_sha256: StrictStr | None = None

    @field_validator("provider_tool_name", "implementation_id", "model_tool_name")
    @classmethod
    def _selection_text_valid(cls, value: str, info: Any) -> str:
        return _safe_connector_string(value, info.field_name, MAX_CONNECTOR_FIELD_UTF8)

    @field_validator("tool_id")
    @classmethod
    def _tool_id_valid(cls, value: str) -> str:
        return validate_canonical_tool_id(value)

    @field_validator("tool_version")
    @classmethod
    def _tool_version_valid(cls, value: int) -> int:
        return validate_tool_version(value)

    @field_validator(
        "expected_raw_tool_sha256",
        "expected_descriptor_sha256",
        "expected_admission_record_sha256",
    )
    @classmethod
    def _expected_hash_valid(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return validate_sha256(value, info.field_name)

    @field_validator("description")
    @classmethod
    def _description_valid(cls, value: str) -> str:
        return _safe_connector_string(
            value, "description", MAX_ADMITTED_DESCRIPTION_UTF8
        )

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _optional_schema_frozen(
        cls, value: Any, info: Any
    ) -> MappingProxyType[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError(f"{info.field_name} must be a JSON object")
        return _freeze_json_object(
            normalize_json_schema(value, field_name=info.field_name)
        )

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

    @model_validator(mode="after")
    def _selection_complete(self) -> ConnectorToolSelection:
        if self.approval_policy is ConnectorApprovalPolicy.FORBIDDEN:
            raise ValueError("forbidden approval policy cannot admit a tool")
        if (
            self.output_schema_policy is OutputSchemaPolicy.OPERATOR_SUPPLIED
            and self.output_schema is None
        ):
            raise ValueError("operator-supplied output schema is required")
        if (
            self.side_effect_class in _SIDE_EFFECTS_REQUIRING_APPROVAL
            and self.approval_policy
            not in {
                ConnectorApprovalPolicy.MILLRACE_EXPLICIT,
                ConnectorApprovalPolicy.OPERATOR_OUT_OF_BAND,
            }
        ):
            raise ValueError("side-effecting connector tools require explicit approval")
        return self

    @field_serializer("input_schema", "output_schema")
    def _serialize_schema(self, value: Any) -> Any:
        return _thaw_json_value(value)


class DeniedConnectorTool(BaseModel):
    """Denied discovered tool and operator review evidence."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        hide_input_in_errors=True,
    )

    provider_tool_name: StrictStr
    reason: StrictStr
    approval_policy: ConnectorApprovalPolicy = ConnectorApprovalPolicy.FORBIDDEN
    review_evidence: Mapping[str, JsonValue] = Field(
        default_factory=lambda: MappingProxyType({})
    )

    @field_validator("provider_tool_name", "reason")
    @classmethod
    def _denial_text_valid(cls, value: str, info: Any) -> str:
        return _safe_connector_string(value, info.field_name, MAX_CONNECTOR_FIELD_UTF8)

    @field_validator("review_evidence")
    @classmethod
    def _review_evidence_frozen(cls, value: Any) -> MappingProxyType[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("review_evidence must be a JSON object")
        _reject_secret_values(value, path="/review_evidence")
        return _freeze_json_object(value)

    @field_serializer("review_evidence")
    def _serialize_review_evidence(self, value: Any) -> Any:
        return _thaw_json_value(value)


class ConnectorAdmissionManifest(BaseModel):
    """Explicit operator admission manifest."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        hide_input_in_errors=True,
    )

    schema_version: StrictStr = CONNECTOR_ADMISSION_MANIFEST_SCHEMA
    kind: StrictStr = CONNECTOR_ADMISSION_MANIFEST_KIND
    version: StrictStr = CONNECTOR_ADMISSION_MANIFEST_VERSION
    connector_id: StrictStr
    expected_identity: ExpectedConnectorIdentity
    selected_tools: tuple[ConnectorToolSelection, ...]
    denied_tools: tuple[DeniedConnectorTool, ...] = Field(default_factory=tuple)
    policy_metadata: Mapping[str, JsonValue] = Field(
        default_factory=lambda: MappingProxyType({})
    )
    expected_connector_identity_sha256: StrictStr | None = None
    expected_discovery_snapshot_sha256: StrictStr | None = None

    @field_validator("schema_version")
    @classmethod
    def _schema_version_valid(cls, value: str) -> str:
        if value != CONNECTOR_ADMISSION_MANIFEST_SCHEMA:
            raise ValueError(
                "schema_version must identify connector admission manifests"
            )
        return value

    @field_validator("kind")
    @classmethod
    def _kind_valid(cls, value: str) -> str:
        if value != CONNECTOR_ADMISSION_MANIFEST_KIND:
            raise ValueError("kind must be connector_admission_manifest")
        return value

    @field_validator("version")
    @classmethod
    def _version_valid(cls, value: str) -> str:
        if value != CONNECTOR_ADMISSION_MANIFEST_VERSION:
            raise ValueError("version must be 1.0")
        return value

    @field_validator("connector_id")
    @classmethod
    def _connector_id_valid(cls, value: str) -> str:
        return _safe_connector_string(value, "connector_id", MAX_CONNECTOR_ID_UTF8)

    @field_validator(
        "expected_connector_identity_sha256",
        "expected_discovery_snapshot_sha256",
    )
    @classmethod
    def _expected_hash_valid(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return validate_sha256(value, info.field_name)

    @field_validator("selected_tools", "denied_tools", mode="before")
    @classmethod
    def _tool_tuples(cls, value: Any, info: Any) -> tuple[Any, ...]:
        if value is None and info.field_name == "denied_tools":
            return ()
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError(f"{info.field_name} must be an array")
        return tuple(value)

    @field_validator("policy_metadata")
    @classmethod
    def _policy_metadata_frozen(cls, value: Any) -> MappingProxyType[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("policy_metadata must be a JSON object")
        _reject_secret_values(value, path="/policy_metadata")
        return _freeze_json_object(value)

    @model_validator(mode="after")
    def _manifest_consistent(self) -> ConnectorAdmissionManifest:
        if not self.selected_tools:
            raise ValueError("selected_tools must contain at least one tool")
        selected_names = tuple(item.provider_tool_name for item in self.selected_tools)
        denied_names = tuple(item.provider_tool_name for item in self.denied_tools)
        validate_unique(selected_names, "selected provider_tool_name")
        validate_unique(denied_names, "denied provider_tool_name")
        overlap = set(selected_names) & set(denied_names)
        if overlap:
            raise ValueError("selected and denied provider tool names must be disjoint")
        tool_keys = tuple(
            f"{item.tool_id}@{item.tool_version}" for item in self.selected_tools
        )
        validate_unique(tool_keys, "selected tool identities")
        model_names = tuple(item.model_tool_name for item in self.selected_tools)
        validate_unique(model_names, "selected model_tool_name")
        implementation_ids = tuple(
            item.implementation_id for item in self.selected_tools
        )
        validate_unique(implementation_ids, "selected implementation_id")
        return self

    @field_serializer("policy_metadata")
    def _serialize_policy_metadata(self, value: Any) -> Any:
        return _thaw_json_value(value)


class ConnectorAdmissionPolicy(BaseModel):
    """Closed policy constraints for offline connector admission."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        hide_input_in_errors=True,
    )

    allowed_capability_ids: tuple[StrictStr, ...]
    allowed_protocols: tuple[ConnectorProtocol, ...] = (ConnectorProtocol.MCP,)
    allowed_transport_kinds: tuple[ConnectorTransportKind, ...] = (
        ConnectorTransportKind.STDIO,
        ConnectorTransportKind.HTTP,
    )
    max_description_utf8: StrictInt = Field(gt=0, le=MAX_ADMITTED_DESCRIPTION_UTF8)
    side_effect_approval_matrix: Mapping[
        SideEffectClass, tuple[ConnectorApprovalPolicy, ...]
    ] = Field(default_factory=lambda: MappingProxyType(_default_approval_matrix()))

    @field_validator("allowed_capability_ids", mode="before")
    @classmethod
    def _allowed_capabilities_snapshot(cls, value: Any) -> tuple[str, ...]:
        values = _sorted_string_tuple(value, "allowed_capability_ids")
        for capability_id in values:
            validate_capability_id(capability_id)
        return validate_unique(values, "allowed_capability_ids")

    @field_validator("allowed_protocols", "allowed_transport_kinds", mode="before")
    @classmethod
    def _enum_tuples(cls, value: Any, info: Any) -> tuple[Any, ...]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError(f"{info.field_name} must be an array")
        return tuple(value)

    @field_validator("side_effect_approval_matrix")
    @classmethod
    def _matrix_frozen(
        cls, value: Any
    ) -> MappingProxyType[SideEffectClass, tuple[ConnectorApprovalPolicy, ...]]:
        if not isinstance(value, Mapping):
            raise ValueError("side_effect_approval_matrix must be an object")
        matrix: dict[SideEffectClass, tuple[ConnectorApprovalPolicy, ...]] = {}
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
                if isinstance(item, ConnectorApprovalPolicy)
                else ConnectorApprovalPolicy(item)
                for item in raw_values
            )
            if ConnectorApprovalPolicy.FORBIDDEN in policies:
                raise ValueError("forbidden approval policy is denial evidence only")
            if (
                key in _SIDE_EFFECTS_REQUIRING_APPROVAL
                and ConnectorApprovalPolicy.NONE in policies
            ):
                raise ValueError(
                    "side-effecting connector tools cannot use approval none"
                )
            matrix[key] = policies
        return MappingProxyType(
            dict(sorted(matrix.items(), key=lambda item: item[0].value))
        )

    @field_serializer("side_effect_approval_matrix")
    def _serialize_matrix(
        self,
        value: MappingProxyType[SideEffectClass, tuple[ConnectorApprovalPolicy, ...]],
    ) -> dict[str, list[str]]:
        return {
            side_effect.value: [policy.value for policy in policies]
            for side_effect, policies in sorted(
                value.items(), key=lambda item: item[0].value
            )
        }


class ConnectorAdmissionRecord(BaseModel):
    """Immutable metadata tying an admitted descriptor back to discovery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector_id: StrictStr
    provider_tool_name: StrictStr
    connector_identity_sha256: StrictStr
    discovery_snapshot_sha256: StrictStr
    raw_tool_sha256: StrictStr
    input_schema_sha256: StrictStr | None = None
    output_schema_sha256: StrictStr | None = None
    provider_description_sha256: StrictStr | None = None
    descriptor_sha256: StrictStr
    required_capabilities: tuple[StrictStr, ...]
    side_effect_class: SideEffectClass
    idempotency: IdempotencyClass
    timeout_policy: ToolTimeoutPolicy
    output_policy: ToolOutputPolicy
    idempotency_key_policy: StrictStr | None = None
    approval_policy: ConnectorApprovalPolicy

    @field_validator("required_capabilities", mode="before")
    @classmethod
    def _capabilities_snapshot(cls, value: Any) -> tuple[str, ...]:
        return _sorted_string_tuple(value, "required_capabilities")

    @field_validator("required_capabilities")
    @classmethod
    def _capabilities_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for capability in value:
            validate_capability_id(capability)
        return validate_unique(value, "required_capabilities")

    @field_validator("idempotency_key_policy")
    @classmethod
    def _idempotency_key_policy_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_nonblank(value, "idempotency_key_policy")

    @field_validator(
        "connector_identity_sha256",
        "discovery_snapshot_sha256",
        "raw_tool_sha256",
        "input_schema_sha256",
        "output_schema_sha256",
        "provider_description_sha256",
        "descriptor_sha256",
    )
    @classmethod
    def _hash_valid(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return validate_sha256(value, info.field_name)

    @property
    def admission_record_sha256(self) -> str:
        return _sha256_hex(
            {
                "kind": CONNECTOR_ADMISSION_RECORD_HASH_KIND,
                "connector_id": self.connector_id,
                "provider_tool_name": self.provider_tool_name,
                "connector_identity_sha256": self.connector_identity_sha256,
                "discovery_snapshot_sha256": self.discovery_snapshot_sha256,
                "raw_tool_sha256": self.raw_tool_sha256,
                "input_schema_sha256": self.input_schema_sha256,
                "output_schema_sha256": self.output_schema_sha256,
                "provider_description_sha256": self.provider_description_sha256,
                "descriptor_sha256": self.descriptor_sha256,
                "required_capabilities": list(self.required_capabilities),
                "side_effect_class": self.side_effect_class.value,
                "idempotency": self.idempotency.value,
                "timeout_policy": self.timeout_policy.model_dump(mode="json"),
                "output_policy": self.output_policy.model_dump(mode="json"),
                "idempotency_key_policy": self.idempotency_key_policy,
                "approval_policy": self.approval_policy.value,
            }
        )


class ConnectorAdmissionResult(BaseModel):
    """Deterministic result container for connector admission attempts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    descriptors: tuple[ToolDescriptor, ...] = Field(default_factory=tuple)
    records: tuple[ConnectorAdmissionRecord, ...] = Field(default_factory=tuple)
    diagnostics: tuple[ConnectorDiagnostic, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _result_consistent(self) -> ConnectorAdmissionResult:
        if self.accepted:
            if self.diagnostics:
                raise ValueError(
                    "accepted admission results cannot contain diagnostics"
                )
            if not self.descriptors:
                raise ValueError("accepted admission results require descriptors")
            if len(self.descriptors) != len(self.records):
                raise ValueError(
                    "accepted admission results require one record per descriptor"
                )
            descriptor_hashes = tuple(
                descriptor.descriptor_sha256 for descriptor in self.descriptors
            )
            record_hashes = tuple(record.descriptor_sha256 for record in self.records)
            if descriptor_hashes != record_hashes:
                raise ValueError("admission records must match descriptor hashes")
        elif self.descriptors or self.records:
            raise ValueError(
                "rejected admission results cannot contain descriptors or records"
            )
        return self


class ConnectorContractValidation(BaseModel):
    """Contract validation result for malformed raw inputs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Any | None = None
    diagnostics: tuple[ConnectorDiagnostic, ...] = Field(default_factory=tuple)

    @property
    def accepted(self) -> bool:
        return self.value is not None and not self.diagnostics


class ConnectorContractModel(BaseModel):
    """Base helper for diagnostic validation of raw connector mappings."""

    model_config = ConfigDict(frozen=True)
    _diagnostic_phase: ClassVar[ConnectorDiagnosticPhase] = (
        ConnectorDiagnosticPhase.DIAGNOSTIC
    )

    @classmethod
    def validate_contract(cls, raw: Any) -> ConnectorContractValidation:
        """Validate raw input and return stable diagnostics instead of exception text."""
        try:
            return ConnectorContractValidation(value=cls.model_validate(raw))
        except ValidationError as exc:
            missing_field = _validation_missing_field(exc)
            return ConnectorContractValidation(
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
            return ConnectorContractValidation(
                diagnostics=(
                    malformed_input_diagnostic(
                        phase=cls._diagnostic_phase,
                        model_name=cls.__name__,
                        code=ConnectorDiagnosticCode.IDENTITY_INVALID,
                    ),
                )
            )


def _default_approval_matrix() -> dict[
    SideEffectClass, tuple[ConnectorApprovalPolicy, ...]
]:
    explicit = (
        ConnectorApprovalPolicy.MILLRACE_EXPLICIT,
        ConnectorApprovalPolicy.OPERATOR_OUT_OF_BAND,
    )
    return {
        SideEffectClass.READ_ONLY: (
            ConnectorApprovalPolicy.NONE,
            ConnectorApprovalPolicy.MILLRACE_EXPLICIT,
            ConnectorApprovalPolicy.OPERATOR_OUT_OF_BAND,
        ),
        SideEffectClass.ARTIFACT_WRITE: explicit,
        SideEffectClass.WORKSPACE_WRITE: explicit,
        SideEffectClass.PROCESS_EXECUTION: explicit,
        SideEffectClass.NETWORK_READ: explicit,
        SideEffectClass.NETWORK_WRITE: explicit,
        SideEffectClass.TERMINAL: explicit,
    }


def _safe_connector_string(value: str, field_name: str, maximum: int) -> str:
    validate_nonblank(value, field_name)
    validate_utf8_size(value, field_name, maximum)
    _reject_secret_material(value, field_name)
    return value


def _validate_utc_timestamp(value: str, field_name: str) -> str:
    validate_nonblank(value, field_name)
    validate_utf8_size(value, field_name, MAX_CONNECTOR_FIELD_UTF8)
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
    if isinstance(value, tuple):
        return [
            _thaw_json_value(item, path=f"{path}/{index}")
            for index, item in enumerate(value)
        ]
    if isinstance(value, list):
        return [
            _thaw_json_value(item, path=f"{path}/{index}")
            for index, item in enumerate(value)
        ]
    return _validate_json_scalar(value, path=path)


def _validate_json_scalar(value: Any, *, path: str) -> str | int | float | bool | None:
    if isinstance(value, str):
        _reject_secret_json_material(value, path=path)
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


def _reject_secret_json_material(value: str, *, path: str) -> None:
    if "configured_secret_refs" in path:
        return
    field_name = path.rsplit("/", 1)[-1] if path else "value"
    if detect_secret_candidate(
        field_path=path or f"/{field_name}",
        field_name=field_name,
        value=value,
        policy=RedactionPolicy(),
    ):
        raise ValueError(f"{path or '/'} contains suspected secret material")


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


def _validation_code(exc: ValidationError) -> ConnectorDiagnosticCode:
    errors = exc.errors()
    if not errors:
        return ConnectorDiagnosticCode.IDENTITY_INVALID
    first = errors[0]
    loc = tuple(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "")).lower()
    error_text = str(errors).lower()
    if "secret material" in error_text:
        return ConnectorDiagnosticCode.SECRET_MATERIAL
    if "forbidden approval policy cannot admit" in error_text:
        return ConnectorDiagnosticCode.FORBIDDEN_TOOL_ADMITTED
    if "selected tool identities" in error_text:
        return ConnectorDiagnosticCode.DUPLICATE_ADMITTED_TOOL
    if "selected model_tool_name" in error_text:
        return ConnectorDiagnosticCode.DUPLICATE_MODEL_TOOL_NAME
    if "selected implementation_id" in error_text:
        return ConnectorDiagnosticCode.DUPLICATE_IMPLEMENTATION_ID
    if "denied provider_tool_name" in error_text or "disjoint" in error_text:
        return ConnectorDiagnosticCode.DENIED_TOOL_INVALID
    if "output schema" in message or "output_schema" in loc:
        return ConnectorDiagnosticCode.OUTPUT_SCHEMA_UNSUPPORTED
    if "input schema" in message or "input_schema" in loc:
        return ConnectorDiagnosticCode.INPUT_SCHEMA_UNSUPPORTED
    if "required_capabilities" in loc:
        return ConnectorDiagnosticCode.CAPABILITY_MISSING
    if "approval_policy" in loc or "side-effecting connector tools" in error_text:
        return ConnectorDiagnosticCode.APPROVAL_POLICY_INVALID
    if "expected_identity" in loc or "connectoridentity" in error_text:
        return ConnectorDiagnosticCode.IDENTITY_INVALID
    return ConnectorDiagnosticCode.IDENTITY_INVALID
