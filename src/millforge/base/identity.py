"""Stable base-runner identity and per-invocation evidence contracts."""

from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files
from typing import Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

import millforge
from millforge.compiled_plan import StageIdentity, calculate_compiled_plan_sha256
from millforge.compiler.catalogs import (
    CatalogLookupClassification,
    ToolCatalogSnapshot,
)
from millforge.compiler.source import HarnessSource
from millforge.compiler.validators import parse_tool_reference
from millforge.contracts import CapabilityEnvelope, SelectedOutputRequirement
from millforge.model_backend import CapabilitySupport, ResolvedModelProfile
from millforge.tools.pi_compat_catalog import (
    create_pi_compat_tool_snapshot,
)

from .composition import MillforgeBaseComponents
from .harness import _TOOL_PACK_ID, millforge_base_harness_source
from .platform import SUPPORTED_PLATFORMS, SupportedPlatform

__all__ = [
    "MillforgeBaseRunnerDescriptor",
    "MillforgeInvocationEvidence",
    "describe_millforge_base",
]

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_DESCRIPTOR_SCHEMA_VERSION = "1.0"
_INVOCATION_SCHEMA_VERSION = "1.2"
_PACKAGE_NAME = "millforge"
_RUNNER_ID = "millforge-base"
_RUNNER_VERSION = 1
_TOOL_PACK_VERSION = 1
_REQUIRED_MODEL_CAPABILITY_IDS = ("tool_calls",)
_ARTIFACT_CONTRACT_VERSION = "millforge.runtime-artifacts.v1"
_PROMPT_CONTRACT_VERSION = "millforge-base.prompt.v1"
_CONTEXT_CONTRACT_VERSION = "millforge-base.context.v1"
_SUPPORTED_PLATFORMS = SUPPORTED_PLATFORMS
_MILLFORGE_BASE_STAGE_IDENTITY = StageIdentity(
    plane="execution",
    node_id="millforge-base",
    stage_kind_id="millforge_base",
)


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _payload_sha256(value: Mapping[str, Any], digest_field: str) -> str:
    payload = {key: item for key, item in value.items() if key != digest_field}
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _with_payload_digest(
    payload: Mapping[str, Any], *, digest_field: str
) -> dict[str, Any]:
    copied = dict(payload)
    copied[digest_field] = _payload_sha256(copied, digest_field)
    return copied


class MillforgeBaseRunnerDescriptor(BaseModel):
    """Immutable installed-package identity for the ``millforge-base`` runner."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    schema_version: Literal["1.0"]
    package_name: StrictStr
    package_version: StrictStr
    runner_id: StrictStr
    runner_version: StrictInt = Field(ge=1)
    harness_id: StrictStr
    harness_version: StrictInt = Field(ge=1)
    tool_pack_id: StrictStr
    tool_pack_version: StrictInt = Field(ge=1)
    required_model_capability_ids: tuple[StrictStr, ...]
    required_capability_ids: tuple[StrictStr, ...]
    legal_terminal_result_ids: tuple[StrictStr, ...]
    artifact_contract_version: StrictStr
    prompt_contract_version: StrictStr
    context_contract_version: StrictStr
    tool_catalog_sha256: StrictStr
    forge_provenance_sha256: StrictStr
    pi_provenance_sha256: StrictStr
    supported_platforms: tuple[SupportedPlatform, ...]
    descriptor_sha256: StrictStr

    @field_validator(
        "tool_catalog_sha256",
        "forge_provenance_sha256",
        "pi_provenance_sha256",
        "descriptor_sha256",
    )
    @classmethod
    def _hashes_are_lowercase_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("descriptor hashes must be lowercase SHA-256 values")
        return value

    @model_validator(mode="after")
    def _digest_matches_payload(self) -> MillforgeBaseRunnerDescriptor:
        payload = self.model_dump(mode="json")
        if self.descriptor_sha256 != _payload_sha256(payload, "descriptor_sha256"):
            raise ValueError("descriptor_sha256 does not match canonical payload")
        return self


class MillforgeInvocationEvidence(BaseModel):
    """Immutable sanitized evidence for one admitted base-runner request."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    schema_version: Literal["1.2"]
    request_id: StrictStr
    run_id: StrictStr
    selected_output_schema_sha256: StrictStr | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    selected_output_required: StrictBool | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    descriptor_sha256: StrictStr
    compiled_plan_sha256: StrictStr
    model_profile_id: StrictStr
    model_behavior_sha256: StrictStr
    capability_envelope_sha256: StrictStr
    effective_prompt_sha256: StrictStr
    context_sha256: StrictStr
    context_file_count: StrictInt = Field(ge=0)
    context_truncated: StrictBool
    prompt_truncated: StrictBool
    cwd_sha256: StrictStr
    invocation_sha256: StrictStr

    @field_validator(
        "selected_output_schema_sha256",
        "descriptor_sha256",
        "compiled_plan_sha256",
        "model_behavior_sha256",
        "capability_envelope_sha256",
        "effective_prompt_sha256",
        "context_sha256",
        "cwd_sha256",
        "invocation_sha256",
    )
    @classmethod
    def _hashes_are_lowercase_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("evidence hashes must be lowercase SHA-256 values")
        return value

    @field_validator("request_id", "run_id")
    @classmethod
    def _correlation_values_are_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence correlation values must be non-empty strings")
        return value

    @model_validator(mode="after")
    def _digest_matches_payload(self) -> MillforgeInvocationEvidence:
        if (self.selected_output_schema_sha256 is None) != (
            self.selected_output_required is None
        ):
            raise ValueError(
                "selected output evidence digest and required state must be paired"
            )
        payload = self.model_dump(mode="json")
        if self.invocation_sha256 != _payload_sha256(payload, "invocation_sha256"):
            raise ValueError("invocation_sha256 does not match canonical payload")
        return self


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("provenance record contains duplicate keys")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"provenance record contains unsupported constant {value}")


def _canonical_provenance_sha256(raw: bytes) -> str:
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("packaged provenance record is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("packaged provenance record must be a JSON object")
    return hashlib.sha256(_canonical_json_bytes(parsed)).hexdigest()


def _provenance_sha256(*parts: str) -> str:
    resource = files("millforge").joinpath(*parts)
    return _canonical_provenance_sha256(resource.read_bytes())


def _required_capabilities_for_source(
    source: HarnessSource,
    tool_snapshot: ToolCatalogSnapshot,
) -> tuple[str, ...]:
    capabilities: set[str] = set()
    seen_refs: set[tuple[str, int]] = set()
    for node in source.graph.nodes:
        reference = parse_tool_reference(node.tool_ref)
        key = (reference.tool_id, reference.version)
        if key in seen_refs:
            raise ValueError("base harness contains a duplicate exact tool reference")
        seen_refs.add(key)
        lookup = tool_snapshot.resolve_exact(reference.tool_id, reference.version)
        if (
            lookup.classification is not CatalogLookupClassification.FOUND
            or lookup.entry is None
        ):
            raise ValueError("base harness tool reference is not in the Pi catalog")
        capabilities.update(lookup.entry.required_capabilities)
    return tuple(sorted(capabilities))


def _static_contract() -> tuple[
    str,
    int,
    str,
    tuple[str, ...],
    tuple[str, ...],
    str,
]:
    source = millforge_base_harness_source(
        model_profile_id="millforge-base.descriptor",
        system_instructions="millforge-base descriptor inspection",
    )
    tool_snapshot = create_pi_compat_tool_snapshot()
    required_capabilities = _required_capabilities_for_source(source, tool_snapshot)
    terminals = tuple(
        sorted(
            node.terminal_result
            for node in source.graph.nodes
            if node.terminal_result is not None
        )
    )
    return (
        source.harness_id,
        source.harness_version,
        _TOOL_PACK_ID,
        required_capabilities,
        terminals,
        tool_snapshot.snapshot_sha256,
    )


def describe_millforge_base() -> MillforgeBaseRunnerDescriptor:
    """Return the side-effect-free installed ``millforge-base`` descriptor."""

    (
        harness_id,
        harness_version,
        tool_pack_id,
        capabilities,
        terminals,
        tool_catalog_sha256,
    ) = _static_contract()
    payload = {
        "schema_version": _DESCRIPTOR_SCHEMA_VERSION,
        "package_name": _PACKAGE_NAME,
        "package_version": millforge.__version__,
        "runner_id": _RUNNER_ID,
        "runner_version": _RUNNER_VERSION,
        "harness_id": harness_id,
        "harness_version": harness_version,
        "tool_pack_id": tool_pack_id,
        "tool_pack_version": _TOOL_PACK_VERSION,
        "required_model_capability_ids": _REQUIRED_MODEL_CAPABILITY_IDS,
        "required_capability_ids": capabilities,
        "legal_terminal_result_ids": terminals,
        "artifact_contract_version": _ARTIFACT_CONTRACT_VERSION,
        "prompt_contract_version": _PROMPT_CONTRACT_VERSION,
        "context_contract_version": _CONTEXT_CONTRACT_VERSION,
        "tool_catalog_sha256": tool_catalog_sha256,
        "forge_provenance_sha256": _provenance_sha256("_forge", "PROVENANCE.json"),
        "pi_provenance_sha256": _provenance_sha256(
            "tools", "pi_compat", "PROVENANCE.json"
        ),
        "supported_platforms": _SUPPORTED_PLATFORMS,
    }
    return MillforgeBaseRunnerDescriptor.model_validate(
        _with_payload_digest(payload, digest_field="descriptor_sha256")
    )


def _model_behavior_payload(profile: ResolvedModelProfile) -> dict[str, Any]:
    payload = profile.model_dump(
        mode="json",
        exclude={"profile_id", "source_name", "source_digest"},
    )
    authentication = dict(payload["authentication"])
    authentication.pop("secret_ref", None)
    payload["authentication"] = authentication
    return payload


def _model_behavior_sha256(profile: ResolvedModelProfile) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(_model_behavior_payload(profile))
    ).hexdigest()


def _capability_envelope_sha256(envelope: CapabilityEnvelope) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(envelope.model_dump(mode="json"))
    ).hexdigest()


def _build_invocation_evidence(
    components: MillforgeBaseComponents,
    descriptor: MillforgeBaseRunnerDescriptor,
    *,
    request_id: str,
    run_id: str,
    selected_output: SelectedOutputRequirement | None = None,
) -> MillforgeInvocationEvidence:
    compiled_payload = components.compiled_plan.model_dump(mode="json")
    if (
        calculate_compiled_plan_sha256(compiled_payload)
        != components.compiled_plan.compiled_sha256
    ):
        raise ValueError("compiled plan digest is invalid")
    metadata = components.metadata
    payload = {
        "schema_version": _INVOCATION_SCHEMA_VERSION,
        "request_id": request_id,
        "run_id": run_id,
        "descriptor_sha256": descriptor.descriptor_sha256,
        "compiled_plan_sha256": components.compiled_plan.compiled_sha256,
        "model_profile_id": components.model_profile.profile_id,
        "model_behavior_sha256": _model_behavior_sha256(components.model_profile),
        "capability_envelope_sha256": _capability_envelope_sha256(
            components.capability_envelope
        ),
        "effective_prompt_sha256": metadata.effective_prompt_sha256,
        "context_sha256": metadata.context_sha256,
        "context_file_count": metadata.context_file_count,
        "context_truncated": metadata.context_truncated,
        "prompt_truncated": metadata.prompt_truncated,
        "cwd_sha256": metadata.cwd_sha256,
    }
    if selected_output is not None:
        payload["selected_output_schema_sha256"] = selected_output.schema_sha256
        payload["selected_output_required"] = selected_output.required
    return MillforgeInvocationEvidence.model_validate(
        _with_payload_digest(payload, digest_field="invocation_sha256")
    )


def _has_valid_descriptor_digest(descriptor: MillforgeBaseRunnerDescriptor) -> bool:
    payload = descriptor.model_dump(mode="json")
    return descriptor.descriptor_sha256 == _payload_sha256(payload, "descriptor_sha256")


def _has_valid_invocation_digest(evidence: MillforgeInvocationEvidence) -> bool:
    payload = evidence.model_dump(mode="json")
    return evidence.invocation_sha256 == _payload_sha256(payload, "invocation_sha256")


def _descriptor_agrees_with_components(
    descriptor: MillforgeBaseRunnerDescriptor,
    components: MillforgeBaseComponents,
) -> bool:
    plan = components.compiled_plan
    metadata = components.metadata
    profile = components.model_profile
    terminals = tuple(sorted(plan.terminal_result_map.values()))
    return (
        descriptor.harness_id == plan.harness_id
        and descriptor.harness_version == plan.harness_version
        and descriptor.tool_pack_id == metadata.tool_pack_id
        and descriptor.tool_catalog_sha256 == components.tool_snapshot.snapshot_sha256
        and descriptor.required_capability_ids == plan.required_capabilities
        and descriptor.legal_terminal_result_ids == terminals
        and all(
            profile.capabilities.state_for(capability_id) is CapabilitySupport.SUPPORTED
            for capability_id in descriptor.required_model_capability_ids
        )
    )
