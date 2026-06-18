"""Connector-scoped broker contracts for runtime invocation."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictStr,
    field_validator,
    model_validator,
)

from millforge import (
    IdempotencyClass,
    SideEffectCertainty,
    ToolExecutionStatus,
)
from millforge.compiler.validators import validate_nonblank, validate_sha256
from millforge.connectors.contracts import MAX_CONNECTOR_ID_UTF8
from millforge.tools.results import ToolExecutionErrorCode


class ConnectorInvocationRequest(BaseModel):
    """Narrow runtime request passed to connector brokers."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    connector_id: StrictStr
    provider_tool_name: StrictStr
    tool_id: StrictStr
    tool_version: int = Field(ge=1)
    descriptor_sha256: StrictStr
    connector_identity_sha256: StrictStr
    discovery_snapshot_sha256: StrictStr
    raw_tool_sha256: StrictStr
    arguments: Mapping[str, Any]
    request_id: StrictStr
    run_id: StrictStr
    stage_plane: StrictStr
    stage_kind_id: StrictStr
    stage_node_id: StrictStr
    timeout_seconds: StrictFloat
    deadline_remaining_seconds: StrictFloat
    cancellation_requested: StrictBool
    cancellation_id: StrictStr
    idempotency_key: StrictStr | None = None

    @field_validator("arguments", mode="before")
    @classmethod
    def _arguments_frozen(cls, value: Any) -> MappingProxyType[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("arguments must be a JSON object")
        return MappingProxyType(dict(value))

    @model_validator(mode="after")
    def _request_valid(self) -> ConnectorInvocationRequest:
        self._validate_strings()
        return self

    @classmethod
    def from_runtime(
        cls,
        *,
        connector_id: str,
        provider_tool_name: str,
        tool_id: str,
        tool_version: int,
        descriptor_sha256: str,
        connector_identity_sha256: str,
        discovery_snapshot_sha256: str,
        raw_tool_sha256: str,
        arguments: Mapping[str, Any],
        request_id: str,
        run_id: str,
        stage_plane: str,
        stage_kind_id: str,
        stage_node_id: str,
        timeout_seconds: float,
        deadline_remaining_seconds: float,
        cancellation_requested: bool,
        cancellation_id: str,
        idempotency_key: str | None,
    ) -> ConnectorInvocationRequest:
        """Build a broker request from runtime-owned values only."""
        return cls(
            connector_id=connector_id,
            provider_tool_name=provider_tool_name,
            tool_id=tool_id,
            tool_version=tool_version,
            descriptor_sha256=descriptor_sha256,
            connector_identity_sha256=connector_identity_sha256,
            discovery_snapshot_sha256=discovery_snapshot_sha256,
            raw_tool_sha256=raw_tool_sha256,
            arguments=MappingProxyType(dict(arguments)),
            request_id=request_id,
            run_id=run_id,
            stage_plane=stage_plane,
            stage_kind_id=stage_kind_id,
            stage_node_id=stage_node_id,
            timeout_seconds=timeout_seconds,
            deadline_remaining_seconds=deadline_remaining_seconds,
            cancellation_requested=cancellation_requested,
            cancellation_id=cancellation_id,
            idempotency_key=idempotency_key,
        )

    def _validate_strings(self) -> None:
        validate_nonblank(self.connector_id, "connector_id")
        validate_nonblank(self.provider_tool_name, "provider_tool_name")
        validate_nonblank(self.tool_id, "tool_id")
        validate_nonblank(self.request_id, "request_id")
        validate_nonblank(self.run_id, "run_id")
        validate_nonblank(self.stage_plane, "stage_plane")
        validate_nonblank(self.stage_kind_id, "stage_kind_id")
        validate_nonblank(self.stage_node_id, "stage_node_id")
        validate_nonblank(self.cancellation_id, "cancellation_id")
        validate_sha256(self.descriptor_sha256, "descriptor_sha256")
        validate_sha256(self.connector_identity_sha256, "connector_identity_sha256")
        validate_sha256(self.discovery_snapshot_sha256, "discovery_snapshot_sha256")
        validate_sha256(self.raw_tool_sha256, "raw_tool_sha256")
        if len(self.connector_id.encode("utf-8")) > MAX_CONNECTOR_ID_UTF8:
            raise ValueError("connector_id exceeds connector limit")
        if self.idempotency_key is not None:
            validate_nonblank(self.idempotency_key, "idempotency_key")


class ConnectorBrokerOutcome(BaseModel):
    """Deterministic broker result before executor output validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ToolExecutionStatus
    summary: StrictStr
    structured_data: Any = Field(default_factory=dict)
    error_code: ToolExecutionErrorCode | None = None
    side_effect_certainty: SideEffectCertainty = SideEffectCertainty.CONFIRMED_ABSENT
    retryable: StrictBool = False


class ConnectorProviderToolEvidence(BaseModel):
    """Broker-exposed provider evidence checked before connector entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector_id: StrictStr
    provider_tool_name: StrictStr
    connector_identity_sha256: StrictStr
    discovery_snapshot_sha256: StrictStr
    raw_tool_sha256: StrictStr
    input_schema_sha256: StrictStr
    output_schema_sha256: StrictStr | None = None
    provider_description_sha256: StrictStr | None = None

    @model_validator(mode="after")
    def _evidence_valid(self) -> ConnectorProviderToolEvidence:
        validate_nonblank(self.connector_id, "connector_id")
        validate_nonblank(self.provider_tool_name, "provider_tool_name")
        validate_sha256(self.connector_identity_sha256, "connector_identity_sha256")
        validate_sha256(self.discovery_snapshot_sha256, "discovery_snapshot_sha256")
        validate_sha256(self.raw_tool_sha256, "raw_tool_sha256")
        validate_sha256(self.input_schema_sha256, "input_schema_sha256")
        if self.output_schema_sha256 is not None:
            validate_sha256(self.output_schema_sha256, "output_schema_sha256")
        if self.provider_description_sha256 is not None:
            validate_sha256(
                self.provider_description_sha256,
                "provider_description_sha256",
            )
        return self


class ConnectorBroker(Protocol):
    """Connector broker interface keyed by connector and provider tool identity."""

    def has_provider_tool(self, connector_id: str, provider_tool_name: str) -> bool:
        """Return whether the scoped provider tool is available."""

    def provider_tool_evidence(
        self,
        connector_id: str,
        provider_tool_name: str,
    ) -> ConnectorProviderToolEvidence | None:
        """Return current provider evidence without invoking the provider tool."""

    def invoke(self, request: ConnectorInvocationRequest) -> ConnectorBrokerOutcome:
        """Invoke the scoped provider tool without receiving runtime internals."""


def connector_idempotency_key(
    *, idempotency: IdempotencyClass, call_id: str
) -> str | None:
    """Return a broker idempotency key only for key-scoped descriptors."""
    if idempotency is IdempotencyClass.IDEMPOTENT_WITH_KEY:
        return call_id
    return None
