"""Runtime-owned connector admission snapshots."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from millforge import IdempotencyClass, SideEffectClass, canonical_json_serialize
from millforge.compiler.catalogs import ToolCatalogSnapshot
from millforge.compiler.validators import validate_canonical_tool_id, validate_sha256
from millforge.connectors.contracts import (
    ConnectorAdmissionRecord,
    ConnectorApprovalPolicy,
)
from millforge.tools.registry import ToolOutputPolicy, ToolTimeoutPolicy


class ConnectorAdmissionSnapshotError(ValueError):
    """Fail-closed connector admission snapshot construction error."""


class ConnectorAdmissionBinding(BaseModel):
    """Frozen runtime binding from a compiled connector descriptor to admission."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_id: StrictStr
    tool_version: StrictInt = Field(ge=1)
    descriptor_sha256: StrictStr
    connector_id: StrictStr
    provider_tool_name: StrictStr
    connector_identity_sha256: StrictStr
    discovery_snapshot_sha256: StrictStr
    raw_tool_sha256: StrictStr
    input_schema_sha256: StrictStr | None = None
    output_schema_sha256: StrictStr | None = None
    provider_description_sha256: StrictStr | None = None
    required_capabilities: tuple[StrictStr, ...]
    side_effect_class: SideEffectClass
    idempotency: IdempotencyClass
    timeout_policy: ToolTimeoutPolicy
    output_policy: ToolOutputPolicy
    idempotency_key_policy: StrictStr | None = None
    approval_policy: ConnectorApprovalPolicy
    admission_record_sha256: StrictStr

    @field_validator("tool_id")
    @classmethod
    def _tool_id_valid(cls, value: str) -> str:
        return validate_canonical_tool_id(value)

    @field_validator(
        "descriptor_sha256",
        "connector_identity_sha256",
        "discovery_snapshot_sha256",
        "raw_tool_sha256",
        "input_schema_sha256",
        "output_schema_sha256",
        "provider_description_sha256",
        "admission_record_sha256",
    )
    @classmethod
    def _hash_valid(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "sha256")
        return validate_sha256(value, str(field_name))


class ConnectorAdmissionSnapshot:
    """Deep-frozen runtime snapshot of accepted connector admission records."""

    def __init__(
        self,
        *,
        records: Iterable[ConnectorAdmissionRecord],
        descriptor_snapshot: ToolCatalogSnapshot,
    ) -> None:
        descriptor_keys_by_hash = _descriptor_keys_by_hash(descriptor_snapshot)
        bindings: dict[tuple[str, int, str], ConnectorAdmissionBinding] = {}
        for source_record in records:
            record = ConnectorAdmissionRecord.model_validate(
                source_record.model_dump(mode="json")
            )
            key_prefix = descriptor_keys_by_hash.get(record.descriptor_sha256)
            if key_prefix is None:
                raise ConnectorAdmissionSnapshotError(
                    "connector admission record is stale for descriptor snapshot"
                )
            tool_id, tool_version = key_prefix
            if not is_connector_tool_id(tool_id):
                raise ConnectorAdmissionSnapshotError(
                    "connector admission record targets a non-connector descriptor"
                )
            key = (tool_id, tool_version, record.descriptor_sha256)
            if key in bindings:
                raise ConnectorAdmissionSnapshotError(
                    "duplicate connector admission record"
                )
            descriptor_entry = descriptor_snapshot.resolve_exact(
                tool_id, tool_version
            ).entry
            if descriptor_entry is None:
                raise ConnectorAdmissionSnapshotError(
                    "connector admission record is stale for descriptor snapshot"
                )
            _validate_record_descriptor_consistency(record, descriptor_entry)
            bindings[key] = ConnectorAdmissionBinding(
                tool_id=tool_id,
                tool_version=tool_version,
                descriptor_sha256=record.descriptor_sha256,
                connector_id=record.connector_id,
                provider_tool_name=record.provider_tool_name,
                connector_identity_sha256=record.connector_identity_sha256,
                discovery_snapshot_sha256=record.discovery_snapshot_sha256,
                raw_tool_sha256=record.raw_tool_sha256,
                input_schema_sha256=record.input_schema_sha256,
                output_schema_sha256=record.output_schema_sha256,
                provider_description_sha256=record.provider_description_sha256,
                required_capabilities=record.required_capabilities,
                side_effect_class=record.side_effect_class,
                idempotency=record.idempotency,
                timeout_policy=record.timeout_policy,
                output_policy=record.output_policy,
                idempotency_key_policy=record.idempotency_key_policy,
                approval_policy=record.approval_policy,
                admission_record_sha256=record.admission_record_sha256,
            )
        self._bindings = MappingProxyType(dict(sorted(bindings.items())))
        self._snapshot_sha256 = _snapshot_sha256(self._bindings.values())

    @property
    def snapshot_sha256(self) -> str:
        """Return a deterministic hash of accepted connector admission bindings."""
        return self._snapshot_sha256

    @property
    def bindings(self) -> tuple[ConnectorAdmissionBinding, ...]:
        """Return frozen connector admission bindings in deterministic order."""
        return tuple(self._bindings.values())

    def resolve(
        self, tool_id: str, tool_version: int, descriptor_sha256: str
    ) -> ConnectorAdmissionBinding | None:
        """Resolve an exact compiled connector descriptor binding."""
        return self._bindings.get((tool_id, tool_version, descriptor_sha256))

    def require(
        self, tool_id: str, tool_version: int, descriptor_sha256: str
    ) -> ConnectorAdmissionBinding:
        """Resolve an exact connector binding or fail construction."""
        binding = self.resolve(tool_id, tool_version, descriptor_sha256)
        if binding is None:
            raise ConnectorAdmissionSnapshotError(
                "compiled connector descriptor is missing admission record"
            )
        return binding


def is_connector_tool_id(tool_id: str) -> bool:
    """Return whether a tool id belongs to the connector descriptor namespace."""
    return tool_id.startswith("connector.")


def _descriptor_keys_by_hash(
    descriptor_snapshot: ToolCatalogSnapshot,
) -> dict[str, tuple[str, int]]:
    records = getattr(descriptor_snapshot, "descriptor_hash_records", None)
    if records is None:
        raise ConnectorAdmissionSnapshotError(
            "descriptor snapshot does not expose descriptor hash records"
        )
    keys_by_hash: dict[str, tuple[str, int]] = {}
    for record in records:
        descriptor_sha256 = record.descriptor_sha256
        key = (record.tool_id, record.tool_version)
        if descriptor_sha256 in keys_by_hash:
            raise ConnectorAdmissionSnapshotError("duplicate descriptor hash record")
        keys_by_hash[descriptor_sha256] = key
    return keys_by_hash


def _validate_record_descriptor_consistency(
    record: ConnectorAdmissionRecord, descriptor_entry: object
) -> None:
    expected = {
        "descriptor_sha256": getattr(descriptor_entry, "descriptor_sha256", None),
        "required_capabilities": tuple(
            getattr(descriptor_entry, "required_capabilities", ())
        ),
        "side_effect_class": getattr(descriptor_entry, "side_effect_class", None),
        "idempotency": getattr(descriptor_entry, "idempotency", None),
        "timeout_policy": getattr(descriptor_entry, "timeout_policy", None),
        "output_policy": getattr(descriptor_entry, "output_policy", None),
        "idempotency_key_policy": (
            "call_id"
            if getattr(descriptor_entry, "idempotency", None)
            is IdempotencyClass.IDEMPOTENT_WITH_KEY
            else None
        ),
    }
    actual = {
        "descriptor_sha256": record.descriptor_sha256,
        "required_capabilities": record.required_capabilities,
        "side_effect_class": record.side_effect_class,
        "idempotency": record.idempotency,
        "timeout_policy": record.timeout_policy,
        "output_policy": record.output_policy,
        "idempotency_key_policy": record.idempotency_key_policy,
    }
    for field, expected_value in expected.items():
        if actual[field] != expected_value:
            raise ConnectorAdmissionSnapshotError(
                f"connector admission record is descriptor-inconsistent: {field}"
            )


def _snapshot_sha256(bindings: Iterable[ConnectorAdmissionBinding]) -> str:
    payload = {
        "kind": "millforge.connector.admission_snapshot.v1",
        "bindings": [
            binding.model_dump(mode="json")
            for binding in sorted(
                bindings,
                key=lambda item: (
                    item.tool_id,
                    item.tool_version,
                    item.descriptor_sha256,
                ),
            )
        ],
    }
    return hashlib.sha256(canonical_json_serialize(payload).encode("utf-8")).hexdigest()
