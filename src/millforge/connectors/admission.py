"""Deterministic offline lowering from connector discovery to tool descriptors."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from millforge.compiler.schema_validation import (
    SchemaSubsetError,
    validate_json_schema_subset,
)
from millforge.compiler.validators import validate_utf8_size
from millforge.connectors.contracts import (
    ConnectorAdmissionManifest,
    ConnectorAdmissionPolicy,
    ConnectorAdmissionRecord,
    ConnectorAdmissionResult,
    ConnectorApprovalPolicy,
    ConnectorDiscoverySnapshot,
    ConnectorToolSelection,
    DescriptionPolicy,
    DiscoveredProviderTool,
    InputSchemaPolicy,
    OutputSchemaPolicy,
    _thaw_json_value,
)
from millforge.connectors.diagnostics import (
    ConnectorDiagnostic,
    ConnectorDiagnosticCode,
    ConnectorDiagnosticPhase,
    connector_diagnostic,
    malformed_input_diagnostic,
)
from millforge.tools import ToolDescriptor

_INSTRUCTION_LIKE_RE = re.compile(
    r"\b(ignore|override|system prompt|developer message|previous instructions|"
    r"follow these instructions|you must|do not tell)\b",
    re.IGNORECASE,
)

_T = TypeVar("_T", bound=BaseModel)


def admit_connector_tools(
    snapshot: ConnectorDiscoverySnapshot | Mapping[str, Any],
    manifest: ConnectorAdmissionManifest | Mapping[str, Any],
    policy: ConnectorAdmissionPolicy | Mapping[str, Any],
) -> ConnectorAdmissionResult:
    """Lower explicit admitted connector tools into immutable descriptors.

    The service treats discovery as untrusted evidence, validates raw mappings
    into connector contracts, and returns stable diagnostics instead of raw
    validation or descriptor-construction exceptions.
    """
    validated = (
        _validate_contract(
            ConnectorDiscoverySnapshot,
            snapshot,
            phase=ConnectorDiagnosticPhase.DISCOVERY,
        ),
        _validate_contract(
            ConnectorAdmissionManifest,
            manifest,
            phase=ConnectorDiagnosticPhase.MANIFEST,
        ),
        _validate_contract(
            ConnectorAdmissionPolicy,
            policy,
            phase=ConnectorDiagnosticPhase.POLICY,
        ),
    )
    diagnostics = tuple(diagnostic for _, diagnostic in validated if diagnostic)
    if diagnostics:
        return ConnectorAdmissionResult(accepted=False, diagnostics=diagnostics)

    valid_snapshot = validated[0][0]
    valid_manifest = validated[1][0]
    valid_policy = validated[2][0]
    if not isinstance(valid_snapshot, ConnectorDiscoverySnapshot):
        raise AssertionError("snapshot validation returned unexpected contract")
    if not isinstance(valid_manifest, ConnectorAdmissionManifest):
        raise AssertionError("manifest validation returned unexpected contract")
    if not isinstance(valid_policy, ConnectorAdmissionPolicy):
        raise AssertionError("policy validation returned unexpected contract")

    admission = _Admission(valid_snapshot, valid_manifest, valid_policy)
    return admission.run()


class _Admission:
    def __init__(
        self,
        snapshot: ConnectorDiscoverySnapshot,
        manifest: ConnectorAdmissionManifest,
        policy: ConnectorAdmissionPolicy,
    ) -> None:
        self.snapshot = snapshot
        self.manifest = manifest
        self.policy = policy
        self.diagnostics: list[ConnectorDiagnostic] = []

    def run(self) -> ConnectorAdmissionResult:
        self._validate_global_inputs()
        provider_tools = {
            tool.provider_tool_name: tool for tool in self.snapshot.provider_tools
        }
        self._validate_denials(provider_tools)
        if any(
            diagnostic.code is not ConnectorDiagnosticCode.HASH_MISMATCH
            for diagnostic in self.diagnostics
        ):
            return self._rejected()

        admitted: list[tuple[ToolDescriptor, ConnectorAdmissionRecord]] = []
        for selection in sorted(
            self.manifest.selected_tools, key=lambda item: item.provider_tool_name
        ):
            provider_tool = provider_tools.get(selection.provider_tool_name)
            if provider_tool is None:
                self._diagnose(
                    ConnectorDiagnosticCode.ADMITTED_PROVIDER_TOOL_MISSING,
                    phase=ConnectorDiagnosticPhase.ADMISSION,
                    path=f"/selected_tools/{selection.provider_tool_name}",
                    message="Selected provider tool is not present in discovery.",
                    evidence={"provider_tool_name": selection.provider_tool_name},
                )
                continue
            lowered = self._lower_selection(selection, provider_tool)
            if lowered is not None:
                admitted.append(lowered)

        if self.diagnostics:
            return self._rejected()

        descriptors = tuple(descriptor for descriptor, _ in admitted)
        records = tuple(record for _, record in admitted)
        return ConnectorAdmissionResult(
            accepted=True,
            descriptors=descriptors,
            records=records,
        )

    def _validate_global_inputs(self) -> None:
        identity = self.snapshot.connector_identity
        identity_sha256 = self._safe_hash(
            lambda: identity.identity_sha256,
            phase=ConnectorDiagnosticPhase.IDENTITY,
            path="/connector_identity_sha256",
            message="Connector identity evidence could not be hashed.",
            evidence={"connector_id": identity.connector_id},
        )
        self._identity_sha256 = identity_sha256
        if self.manifest.connector_id != identity.connector_id:
            self._diagnose(
                ConnectorDiagnosticCode.CONNECTOR_ID_MISMATCH,
                phase=ConnectorDiagnosticPhase.IDENTITY,
                path="/connector_id",
                message="Connector ID does not match discovery identity.",
                evidence={
                    "manifest_connector_id": self.manifest.connector_id,
                    "snapshot_connector_id": identity.connector_id,
                },
            )
        if not self.manifest.expected_identity.matches(identity):
            self._diagnose(
                ConnectorDiagnosticCode.EXPECTED_IDENTITY_MISMATCH,
                phase=ConnectorDiagnosticPhase.IDENTITY,
                path="/expected_identity",
                message="Connector identity does not match the admission manifest.",
                evidence={"connector_id": identity.connector_id},
            )
        if identity_sha256 is not None:
            self._check_hash(
                supplied=self.manifest.expected_connector_identity_sha256,
                actual=identity_sha256,
                path="/expected_connector_identity_sha256",
                label="connector_identity_sha256",
            )
        if identity.protocol not in self.policy.allowed_protocols:
            self._diagnose(
                ConnectorDiagnosticCode.PROTOCOL_UNSUPPORTED,
                phase=ConnectorDiagnosticPhase.POLICY,
                path="/allowed_protocols",
                message="Connector protocol is not allowed by admission policy.",
                evidence={"protocol": identity.protocol.value},
            )
        if identity.transport_kind not in self.policy.allowed_transport_kinds:
            self._diagnose(
                ConnectorDiagnosticCode.TRANSPORT_UNSUPPORTED,
                phase=ConnectorDiagnosticPhase.POLICY,
                path="/allowed_transport_kinds",
                message="Connector transport is not allowed by admission policy.",
                evidence={"transport_kind": identity.transport_kind.value},
            )
        for name in self.snapshot.duplicate_provider_names:
            self._diagnose(
                ConnectorDiagnosticCode.DISCOVERY_DUPLICATE_PROVIDER_TOOL,
                phase=ConnectorDiagnosticPhase.DISCOVERY,
                path="/provider_tools",
                message="Discovery contains duplicate provider tool names.",
                evidence={"provider_tool_name": name},
            )
        snapshot_sha256 = self._safe_hash(
            lambda: self.snapshot.discovery_snapshot_sha256,
            phase=ConnectorDiagnosticPhase.DISCOVERY,
            path="/discovery_snapshot_sha256",
            message="Discovery evidence could not be hashed.",
            evidence={"connector_id": identity.connector_id},
        )
        self._discovery_snapshot_sha256 = snapshot_sha256
        if snapshot_sha256 is not None:
            self._check_hash(
                supplied=self.manifest.expected_discovery_snapshot_sha256,
                actual=snapshot_sha256,
                path="/expected_discovery_snapshot_sha256",
                label="discovery_snapshot_sha256",
            )

    def _validate_denials(
        self, provider_tools: Mapping[str, DiscoveredProviderTool]
    ) -> None:
        for denial in self.manifest.denied_tools:
            if denial.provider_tool_name not in provider_tools:
                self._diagnose(
                    ConnectorDiagnosticCode.DENIED_TOOL_INVALID,
                    phase=ConnectorDiagnosticPhase.MANIFEST,
                    path=f"/denied_tools/{denial.provider_tool_name}",
                    message="Denied provider tool is not present in discovery.",
                    evidence={"provider_tool_name": denial.provider_tool_name},
                )
            if denial.approval_policy is not ConnectorApprovalPolicy.FORBIDDEN:
                self._diagnose(
                    ConnectorDiagnosticCode.DENIED_TOOL_INVALID,
                    phase=ConnectorDiagnosticPhase.MANIFEST,
                    path=f"/denied_tools/{denial.provider_tool_name}/approval_policy",
                    message="Denied connector tools must use forbidden approval policy.",
                    evidence={"provider_tool_name": denial.provider_tool_name},
                )

    def _lower_selection(
        self,
        selection: ConnectorToolSelection,
        provider_tool: DiscoveredProviderTool,
    ) -> tuple[ToolDescriptor, ConnectorAdmissionRecord] | None:
        raw_tool_sha256 = self._safe_hash(
            lambda: provider_tool.raw_tool_sha256,
            phase=ConnectorDiagnosticPhase.DISCOVERY,
            path=f"/provider_tools/{provider_tool.provider_tool_name}/raw_tool_sha256",
            message="Provider tool evidence could not be hashed.",
            evidence={"provider_tool_name": provider_tool.provider_tool_name},
        )
        if raw_tool_sha256 is None:
            return None
        self._check_hash(
            supplied=selection.expected_raw_tool_sha256,
            actual=raw_tool_sha256,
            path=f"/selected_tools/{selection.provider_tool_name}/expected_raw_tool_sha256",
            label="raw_tool_sha256",
        )
        if self._has_blocking_diagnostics():
            return None
        description = self._admitted_description(selection, provider_tool)
        input_schema = self._admitted_input_schema(selection, provider_tool)
        output_schema = self._admitted_output_schema(selection, provider_tool)
        self._validate_capabilities(selection)
        self._validate_approval(selection)
        if self._has_blocking_diagnostics():
            return None
        if description is None or input_schema is None or output_schema is None:
            return None

        try:
            descriptor = ToolDescriptor(
                tool_id=selection.tool_id,
                tool_version=selection.tool_version,
                implementation_id=selection.implementation_id,
                model_tool_name=selection.model_tool_name,
                description=description,
                input_schema=input_schema,
                output_schema=output_schema,
                required_capabilities=selection.required_capabilities,
                produced_artifact_ids=selection.produced_artifact_ids,
                side_effect_class=selection.side_effect_class,
                idempotency=selection.idempotency,
                timeout_policy=selection.timeout_policy,
                output_policy=selection.output_policy,
            )
        except Exception as exc:
            self._diagnose(
                ConnectorDiagnosticCode.APPROVAL_POLICY_INVALID,
                phase=ConnectorDiagnosticPhase.ADMISSION,
                path=f"/selected_tools/{selection.provider_tool_name}",
                message="Admitted connector descriptor is invalid.",
                evidence={"error_type": type(exc).__name__},
            )
            return None

        self._check_hash(
            supplied=selection.expected_descriptor_sha256,
            actual=descriptor.descriptor_sha256,
            path=f"/selected_tools/{selection.provider_tool_name}/expected_descriptor_sha256",
            label="descriptor_sha256",
        )
        record = ConnectorAdmissionRecord(
            connector_id=self.manifest.connector_id,
            provider_tool_name=selection.provider_tool_name,
            connector_identity_sha256=(
                getattr(self, "_identity_sha256", None)
                or self.snapshot.connector_identity.identity_sha256
            ),
            discovery_snapshot_sha256=(
                getattr(self, "_discovery_snapshot_sha256", None)
                or self.snapshot.discovery_snapshot_sha256
            ),
            raw_tool_sha256=raw_tool_sha256,
            input_schema_sha256=provider_tool.input_schema_sha256,
            output_schema_sha256=provider_tool.output_schema_sha256,
            provider_description_sha256=provider_tool.provider_description_sha256,
            descriptor_sha256=descriptor.descriptor_sha256,
            required_capabilities=descriptor.required_capabilities,
            side_effect_class=descriptor.side_effect_class,
            idempotency=descriptor.idempotency,
            timeout_policy=descriptor.timeout_policy,
            output_policy=descriptor.output_policy,
            idempotency_key_policy=_idempotency_key_policy(descriptor.idempotency),
            approval_policy=selection.approval_policy,
        )
        self._check_hash(
            supplied=selection.expected_admission_record_sha256,
            actual=record.admission_record_sha256,
            path=f"/selected_tools/{selection.provider_tool_name}/expected_admission_record_sha256",
            label="admission_record_sha256",
        )
        return descriptor, record

    def _admitted_description(
        self,
        selection: ConnectorToolSelection,
        provider_tool: DiscoveredProviderTool,
    ) -> str | None:
        if selection.description_policy is DescriptionPolicy.OPERATOR_SUPPLIED:
            return self._normalize_description(
                selection.description,
                path=f"/selected_tools/{selection.provider_tool_name}/description",
            )
        if selection.description_policy is DescriptionPolicy.PROVIDER_REJECTED:
            self._diagnose(
                ConnectorDiagnosticCode.DESCRIPTION_REQUIRES_OPERATOR_TEXT,
                phase=ConnectorDiagnosticPhase.MANIFEST,
                path=f"/selected_tools/{selection.provider_tool_name}/description_policy",
                message="Rejected provider descriptions require operator-supplied text.",
                evidence={"provider_tool_name": selection.provider_tool_name},
            )
            return None

        raw_description = provider_tool.provider_description
        if _description_requires_operator_text(
            raw_description,
            maximum=self.policy.max_description_utf8,
        ):
            self._diagnose(
                ConnectorDiagnosticCode.DESCRIPTION_REQUIRES_OPERATOR_TEXT,
                phase=ConnectorDiagnosticPhase.ADMISSION,
                path=f"/provider_tools/{provider_tool.provider_tool_name}/provider_description",
                message="Provider description requires operator-supplied text.",
                evidence={"provider_tool_name": provider_tool.provider_tool_name},
            )
            return None
        normalized = self._normalize_description(
            raw_description,
            path=f"/provider_tools/{provider_tool.provider_tool_name}/provider_description",
        )
        if normalized != selection.description:
            self._diagnose(
                ConnectorDiagnosticCode.DESCRIPTION_REQUIRES_OPERATOR_TEXT,
                phase=ConnectorDiagnosticPhase.MANIFEST,
                path=f"/selected_tools/{selection.provider_tool_name}/description",
                message="Sanitized provider description does not match manifest text.",
                evidence={"provider_tool_name": selection.provider_tool_name},
            )
            return None
        return normalized

    def _admitted_input_schema(
        self,
        selection: ConnectorToolSelection,
        provider_tool: DiscoveredProviderTool,
    ) -> MappingProxyType[str, Any] | None:
        if selection.input_schema_policy is InputSchemaPolicy.OPERATOR_OVERLAY:
            if selection.input_schema is None:
                self._diagnose(
                    ConnectorDiagnosticCode.INPUT_SCHEMA_UNSUPPORTED,
                    phase=ConnectorDiagnosticPhase.MANIFEST,
                    path=f"/selected_tools/{selection.provider_tool_name}/input_schema",
                    message="Operator overlay input schema is required.",
                    evidence={"provider_tool_name": selection.provider_tool_name},
                )
                return None
            return _schema_dict(selection.input_schema)
        provider_schema = _normalize_schema_or_diagnose(
            provider_tool.input_schema,
            field_name="input_schema",
            phase=ConnectorDiagnosticPhase.ADMISSION,
            path=f"/provider_tools/{provider_tool.provider_tool_name}/input_schema",
            diagnose=self._diagnose,
        )
        if provider_schema is not None and selection.input_schema is not None:
            selected_schema = _schema_dict(selection.input_schema)
            if selected_schema != provider_schema:
                self._diagnose(
                    ConnectorDiagnosticCode.HASH_MISMATCH,
                    phase=ConnectorDiagnosticPhase.MANIFEST,
                    path=f"/selected_tools/{selection.provider_tool_name}/input_schema",
                    message="Manifest input schema does not match provider schema.",
                    evidence={"provider_tool_name": selection.provider_tool_name},
                )
                return None
        return provider_schema

    def _admitted_output_schema(
        self,
        selection: ConnectorToolSelection,
        provider_tool: DiscoveredProviderTool,
    ) -> MappingProxyType[str, Any] | None:
        if selection.output_schema_policy is OutputSchemaPolicy.OPERATOR_SUPPLIED:
            if selection.output_schema is None:
                self._diagnose(
                    ConnectorDiagnosticCode.OUTPUT_SCHEMA_UNSUPPORTED,
                    phase=ConnectorDiagnosticPhase.MANIFEST,
                    path=f"/selected_tools/{selection.provider_tool_name}/output_schema",
                    message="Operator-supplied output schema is required.",
                    evidence={"provider_tool_name": selection.provider_tool_name},
                )
                return None
            return _schema_dict(selection.output_schema)
        if provider_tool.output_schema is None:
            self._diagnose(
                ConnectorDiagnosticCode.OUTPUT_SCHEMA_UNSUPPORTED,
                phase=ConnectorDiagnosticPhase.ADMISSION,
                path=f"/provider_tools/{provider_tool.provider_tool_name}/output_schema",
                message="Provider output schema is required unless operator supplied.",
                evidence={"provider_tool_name": provider_tool.provider_tool_name},
            )
            return None
        provider_schema = _normalize_schema_or_diagnose(
            provider_tool.output_schema,
            field_name="output_schema",
            phase=ConnectorDiagnosticPhase.ADMISSION,
            path=f"/provider_tools/{provider_tool.provider_tool_name}/output_schema",
            diagnose=self._diagnose,
        )
        if provider_schema is not None and selection.output_schema is not None:
            selected_schema = _schema_dict(selection.output_schema)
            if selected_schema != provider_schema:
                self._diagnose(
                    ConnectorDiagnosticCode.HASH_MISMATCH,
                    phase=ConnectorDiagnosticPhase.MANIFEST,
                    path=f"/selected_tools/{selection.provider_tool_name}/output_schema",
                    message="Manifest output schema does not match provider schema.",
                    evidence={"provider_tool_name": selection.provider_tool_name},
                )
                return None
        return provider_schema

    def _validate_capabilities(self, selection: ConnectorToolSelection) -> None:
        if not selection.required_capabilities:
            self._diagnose(
                ConnectorDiagnosticCode.CAPABILITY_MISSING,
                phase=ConnectorDiagnosticPhase.POLICY,
                path=f"/selected_tools/{selection.provider_tool_name}/required_capabilities",
                message="Selected connector tool requires explicit capabilities.",
                evidence={"provider_tool_name": selection.provider_tool_name},
            )
            return
        allowed = set(self.policy.allowed_capability_ids)
        for capability_id in selection.required_capabilities:
            if capability_id not in allowed:
                self._diagnose(
                    ConnectorDiagnosticCode.CAPABILITY_UNKNOWN,
                    phase=ConnectorDiagnosticPhase.POLICY,
                    path=f"/selected_tools/{selection.provider_tool_name}/required_capabilities",
                    message="Connector capability is not allowed by admission policy.",
                    evidence={"capability_id": capability_id},
                )

    def _has_blocking_diagnostics(self) -> bool:
        return any(
            diagnostic.code is not ConnectorDiagnosticCode.HASH_MISMATCH
            for diagnostic in self.diagnostics
        )

    def _safe_hash(
        self,
        supplier: Any,
        *,
        phase: ConnectorDiagnosticPhase,
        path: str,
        message: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> str | None:
        try:
            return supplier()
        except Exception as exc:
            code = (
                ConnectorDiagnosticCode.SECRET_MATERIAL
                if "secret material" in str(exc).lower()
                else ConnectorDiagnosticCode.IDENTITY_INVALID
            )
            self._diagnose(
                code,
                phase=phase,
                path=path,
                message=(
                    "Discovery evidence contains suspected secret material."
                    if code is ConnectorDiagnosticCode.SECRET_MATERIAL
                    else message
                ),
                evidence={
                    **(evidence or {}),
                    "error_type": type(exc).__name__,
                },
            )
            return None

    def _validate_approval(self, selection: ConnectorToolSelection) -> None:
        allowed = self.policy.side_effect_approval_matrix.get(
            selection.side_effect_class
        )
        if allowed is None or selection.approval_policy not in allowed:
            self._diagnose(
                ConnectorDiagnosticCode.APPROVAL_POLICY_INVALID,
                phase=ConnectorDiagnosticPhase.POLICY,
                path=f"/selected_tools/{selection.provider_tool_name}/approval_policy",
                message="Approval policy is not allowed for side-effect class.",
                evidence={
                    "approval_policy": selection.approval_policy.value,
                    "side_effect_class": selection.side_effect_class.value,
                },
            )

    def _normalize_description(self, value: str, *, path: str) -> str | None:
        normalized = " ".join(value.split())
        try:
            validate_utf8_size(
                normalized, "description", self.policy.max_description_utf8
            )
        except Exception:
            self._diagnose(
                ConnectorDiagnosticCode.DESCRIPTION_REQUIRES_OPERATOR_TEXT,
                phase=ConnectorDiagnosticPhase.MANIFEST,
                path=path,
                message="Connector description exceeds admission policy.",
            )
            return None
        return normalized

    def _check_hash(
        self,
        *,
        supplied: str | None,
        actual: str,
        path: str,
        label: str,
    ) -> None:
        if supplied is not None and supplied != actual:
            self._diagnose(
                ConnectorDiagnosticCode.HASH_MISMATCH,
                phase=ConnectorDiagnosticPhase.ADMISSION,
                path=path,
                message="Supplied connector hash does not match recomputed hash.",
                evidence={"hash": label},
            )

    def _diagnose(
        self,
        code: ConnectorDiagnosticCode,
        *,
        phase: ConnectorDiagnosticPhase,
        message: str,
        location: str | None = None,
        path: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        self.diagnostics.append(
            connector_diagnostic(
                code,
                phase=phase,
                message=message,
                location=location,
                path=path,
                evidence=evidence,
            )
        )

    def _rejected(self) -> ConnectorAdmissionResult:
        return ConnectorAdmissionResult(
            accepted=False,
            diagnostics=tuple(
                sorted(
                    self.diagnostics,
                    key=lambda item: (
                        item.code.value,
                        item.phase.value,
                        item.path or "",
                        item.location or "",
                        item.message,
                    ),
                )
            ),
        )


def _validate_contract(
    model: type[_T],
    value: _T | Mapping[str, Any],
    *,
    phase: ConnectorDiagnosticPhase,
) -> tuple[_T | None, ConnectorDiagnostic | None]:
    if isinstance(value, model):
        return value, None
    try:
        return model.model_validate(value), None
    except ValidationError as exc:
        schema_diagnostic = _schema_subset_validation_diagnostic(
            exc,
            model_name=model.__name__,
            phase=phase,
        )
        if schema_diagnostic is not None:
            return None, schema_diagnostic
        missing_field = _missing_field(exc)
        return (
            None,
            malformed_input_diagnostic(
                phase=phase,
                model_name=model.__name__,
                path=_validation_pointer(exc),
                missing_field=missing_field,
                code=_validation_code(exc),
            ),
        )
    except Exception:
        return (
            None,
            malformed_input_diagnostic(
                phase=phase,
                model_name=model.__name__,
                code=ConnectorDiagnosticCode.IDENTITY_INVALID,
            ),
        )


def _normalize_schema_or_diagnose(
    schema: Mapping[str, Any],
    *,
    field_name: str,
    phase: ConnectorDiagnosticPhase,
    path: str,
    diagnose: Any,
) -> MappingProxyType[str, Any] | None:
    try:
        return validate_json_schema_subset(
            _thaw_json_value(schema), field_name=field_name
        )
    except Exception as exc:
        diagnose(
            (
                ConnectorDiagnosticCode.INPUT_SCHEMA_UNSUPPORTED
                if field_name == "input_schema"
                else ConnectorDiagnosticCode.OUTPUT_SCHEMA_UNSUPPORTED
            ),
            phase=phase,
            path=path,
            message="Connector schema is outside the accepted JSON Schema subset.",
            evidence={
                "error_type": type(exc).__name__,
                "schema_error": str(exc),
            },
        )
    return None


def _schema_subset_validation_diagnostic(
    exc: ValidationError,
    *,
    model_name: str,
    phase: ConnectorDiagnosticPhase,
) -> ConnectorDiagnostic | None:
    for error in exc.errors():
        ctx = error.get("ctx")
        if not isinstance(ctx, Mapping):
            continue
        schema_error = ctx.get("error")
        if not isinstance(schema_error, SchemaSubsetError):
            continue
        loc = error.get("loc")
        path = _validation_pointer_from_loc(loc)
        code = (
            ConnectorDiagnosticCode.OUTPUT_SCHEMA_UNSUPPORTED
            if _validation_loc_has_field(loc, "output_schema")
            else ConnectorDiagnosticCode.INPUT_SCHEMA_UNSUPPORTED
        )
        return connector_diagnostic(
            code,
            phase=phase,
            path=path,
            message="Connector schema is outside the accepted JSON Schema subset.",
            evidence={
                "model": model_name,
                "error_type": type(schema_error).__name__,
                "schema_error": str(schema_error),
            },
        )
    return None


def _idempotency_key_policy(idempotency: Any) -> str | None:
    from millforge import IdempotencyClass

    if idempotency is IdempotencyClass.IDEMPOTENT_WITH_KEY:
        return "call_id"
    return None


def _schema_dict(schema: Mapping[str, Any]) -> MappingProxyType[str, Any]:
    return validate_json_schema_subset(_thaw_json_value(schema))


def _description_requires_operator_text(value: str, *, maximum: int) -> bool:
    if len(value.encode("utf-8")) > maximum:
        return True
    return _INSTRUCTION_LIKE_RE.search(value) is not None


def _validation_pointer(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "/"
    loc = errors[0].get("loc")
    return _validation_pointer_from_loc(loc)


def _validation_pointer_from_loc(loc: Any) -> str:
    if not isinstance(loc, tuple | list) or not loc:
        return "/"
    parts = [str(part).replace("~", "~0").replace("/", "~1") for part in loc]
    return "/" + "/".join(parts)


def _validation_loc_has_field(loc: Any, field_name: str) -> bool:
    if not isinstance(loc, tuple | list):
        return False
    return any(str(part) == field_name for part in loc)


def _missing_field(exc: ValidationError) -> str | None:
    errors = exc.errors()
    if not errors or errors[0].get("type") != "missing":
        return None
    loc = errors[0].get("loc")
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
