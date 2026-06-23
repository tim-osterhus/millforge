"""Static compact-eval mode descriptors for Pi and Millforge runners."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from millforge.eval_artifacts import (
    EvalArtifactLayoutEntry,
    EvalValidatorVisibilityRecord,
    canonical_eval_artifact_layout,
)
from millforge.eval_boundary import (
    EvalBoundaryBaseline,
    EvalCapabilityEnvelope,
    EvalContextTier,
    EvalFixtureWorkspacePolicy,
    EvalResourceCeiling,
    EvalStageContextPolicy,
    compact_eval_boundary_baseline,
    default_eval_capability_envelopes,
    default_eval_stage_context_policies,
    default_eval_trial_resource_ceiling,
)
from millforge.eval_workflow import (
    EvalStageContract,
    EvalStageId,
    EvalTerminalResult,
    compact_eval_workflow_snapshot,
    default_compact_eval_workflow_graph,
)

EVAL_MODE_SCHEMA_VERSION = 1
EVAL_MODE_FINGERPRINT_KIND = "eval_mode_descriptor_sha256_v1"
EVAL_MODE_FAIRNESS_FINGERPRINT_KIND = "eval_mode_fairness_sha256_v1"
EVAL_MODEL_PROFILE_HASH_KIND = "eval_model_profile_sha256_v1"
EVAL_SMALL_PI_MODE_ID = "eval_small_pi"
EVAL_SMALL_MILLFORGE_MODE_ID = "eval_small_millforge"
EVAL_DEFAULT_MODEL_PROFILE_ID = "eval.backend_neutral.default.v1"
EVAL_CLOSURE_BOUNDARY_ID = "millforge.eval_boundary.validate_eval_closure.v1"
EVAL_COMPARISON_ENGINEERING_SMOKE_ONLY = "engineering_smoke_only"
EVAL_SPEC_07_HARNESS_IDS: Mapping[EvalStageId, str] = {
    EvalStageId.PLANNER: "millforge.eval.planner.single_task.v1",
    EvalStageId.BUILDER: "millforge.eval.builder.code_patch.v1",
    EvalStageId.CHECKER: "millforge.eval.checker.evidence_review.v1",
    EvalStageId.ARBITER: "millforge.eval.arbiter.closure.v1",
}
EVAL_SPEC_07_HARNESS_IDS = MappingProxyType(dict(EVAL_SPEC_07_HARNESS_IDS))
_EVAL_SPEC_07_IMPLEMENTED_HARNESS_STAGES = (
    EvalStageId.PLANNER,
    EvalStageId.BUILDER,
    EvalStageId.CHECKER,
    EvalStageId.ARBITER,
)

_SERVING_CLASSES = frozenset({"hosted_api", "local_openai_compatible", "local_native"})
_SERVING_PROTOCOLS = frozenset(
    {
        "openai_compatible_chat",
        "openai_compatible_responses",
        "native_provider",
        "offline_fake",
    }
)
_TOOL_CALLING_MODES = frozenset({"native", "parser", "unsupported"})
_CONFOUND_SEVERITIES = frozenset({"info", "warning", "invalidating"})
_CONFOUND_KINDS = frozenset(
    {
        "runner_capability_enforcement",
        "tool_calling",
        "parser_fallback",
        "context_packing",
        "model_endpoint",
        "token_accounting",
        "wall_clock_measurement",
        "deferred_millforge_harness",
        "deferred_pi_runtime",
    }
)
_DEPENDENCY_KINDS = frozenset(
    {
        "runner_runtime",
        "runner_harness",
        "model_backend",
        "resource_enforcement",
        "fixture_workspace",
    }
)
_DEPENDENCY_AFFECTED_MODES = frozenset(
    {
        EVAL_SMALL_PI_MODE_ID,
        EVAL_SMALL_MILLFORGE_MODE_ID,
        "all_modes",
    }
)
_LIVE_DEPENDENCY_IDS = (
    "pi_live_runtime_support",
    "spec_07_harness_presets",
    "model_backend_configuration",
    "resource_ceiling_enforcement",
    "fixture_workspace_creation",
)
_ALLOWED_RUNNER_DIFFERENCE_FIELDS = (
    "mode_id",
    "runner_bindings",
    "deferred_dependencies",
)
_DENIED_DESCRIPTOR_TOKENS = (
    "api_key",
    "credential",
    "password",
    "/mnt/f",
    "f:\\",
    "/home/",
    "\\users\\",
    "millrace-agents",
    "ideas/",
    "ref-forge/",
    "hidden scorer",
    "hidden_scorer",
)
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s\"'`([{<])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'`)\]}>,]*"
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?:^|[\s\"'`([{<])/(?!/)[^\s\"'`)\]}>,]*")
_USER_HOME_PATH = re.compile(r"(?:^|[\s\"'`([{<])~(?:/|\\)[^\s\"'`)\]}>,]*")


class _FrozenDescriptorDict(dict[Any, Any]):
    """Dict-shaped immutable mapping that remains serializable by Pydantic."""

    def __readonly(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("eval descriptor mappings are immutable")

    __setitem__ = __readonly
    __delitem__ = __readonly
    clear = __readonly
    pop = __readonly
    popitem = __readonly  # type: ignore[assignment]
    setdefault = __readonly
    update = __readonly
    __ior__ = __readonly  # type: ignore[assignment]


class EvalRunnerKind(str, Enum):
    """Closed runner kinds for compact eval modes."""

    PI = "pi"
    MILLFORGE = "millforge"


class EvalRunnerBinding(BaseModel):
    """Closed binding from a compact eval stage to one runner kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_id: EvalStageId
    runner_kind: EvalRunnerKind
    harness_id: StrictStr | None = None

    @model_validator(mode="after")
    def _binding_valid(self) -> EvalRunnerBinding:
        if self.runner_kind == EvalRunnerKind.PI:
            if self.harness_id is not None:
                raise ValueError("pi runner bindings must not declare harness_id")
        elif self.harness_id != EVAL_SPEC_07_HARNESS_IDS[self.stage_id]:
            raise ValueError("millforge runner binding has an unknown harness_id")
        return self


class EvalModelProfile(BaseModel):
    """Backend-neutral model profile included in compact eval descriptors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: StrictStr
    provider_label: StrictStr
    model_label: StrictStr
    serving_class: StrictStr
    serving_protocol: StrictStr
    tool_calling_mode: StrictStr
    parser_id: StrictStr
    reasoning_effort: StrictStr
    temperature: StrictFloat = Field(ge=0.0, le=2.0)
    top_p: StrictFloat = Field(gt=0.0, le=1.0)
    max_prompt_tokens: StrictInt = Field(gt=0)
    max_completion_tokens: StrictInt = Field(gt=0)
    max_total_tokens: StrictInt = Field(gt=0)
    max_model_calls: StrictInt = Field(gt=0)
    cost_accounting: Mapping[StrictStr, StrictStr]
    model_profile_hash_kind: StrictStr = EVAL_MODEL_PROFILE_HASH_KIND
    model_profile_hash: StrictStr

    @field_validator(
        "profile_id",
        "provider_label",
        "model_label",
        "parser_id",
        "reasoning_effort",
    )
    @classmethod
    def _stable_text_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model profile text fields must be non-empty")
        _reject_descriptor_material_leaks(value)
        return value

    @field_validator("serving_class")
    @classmethod
    def _serving_class_valid(cls, value: str) -> str:
        if value not in _SERVING_CLASSES:
            raise ValueError("unsupported eval model serving_class")
        return value

    @field_validator("serving_protocol")
    @classmethod
    def _serving_protocol_valid(cls, value: str) -> str:
        if value not in _SERVING_PROTOCOLS:
            raise ValueError("unsupported eval model serving_protocol")
        return value

    @field_validator("tool_calling_mode")
    @classmethod
    def _tool_calling_mode_valid(cls, value: str) -> str:
        if value not in _TOOL_CALLING_MODES:
            raise ValueError("unsupported eval model tool_calling_mode")
        return value

    @model_validator(mode="after")
    def _model_profile_valid(self) -> EvalModelProfile:
        if self.max_total_tokens != self.max_prompt_tokens + self.max_completion_tokens:
            raise ValueError(
                "max_total_tokens must equal prompt plus completion token limits"
            )
        object.__setattr__(
            self,
            "cost_accounting",
            _freeze_descriptor_mapping(self.cost_accounting),
        )
        if self.model_profile_hash_kind != EVAL_MODEL_PROFILE_HASH_KIND:
            raise ValueError("unsupported model profile hash kind")
        _validate_sha256(self.model_profile_hash)
        expected = calculate_eval_model_profile_hash(self)
        if self.model_profile_hash != expected:
            raise ValueError("model_profile_hash does not match profile payload")
        _reject_descriptor_material_leaks(self.model_dump(mode="json"))
        return self


class EvalModeDeferredDependency(BaseModel):
    """Static descriptor reference to an intentionally deferred implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dependency_id: StrictStr
    dependency_kind: StrictStr
    affected_mode: StrictStr
    affected_stage_id: EvalStageId | None = None
    all_stage_scope: StrictBool = True
    static_descriptor_admission_behavior: StrictStr
    live_execution_behavior: StrictStr
    summary: StrictStr
    reference_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    required_before_live_execution: bool = True

    @field_validator("dependency_kind")
    @classmethod
    def _dependency_kind_valid(cls, value: str) -> str:
        if value not in _DEPENDENCY_KINDS:
            raise ValueError("unsupported deferred dependency kind")
        return value

    @field_validator("affected_mode")
    @classmethod
    def _affected_mode_valid(cls, value: str) -> str:
        if value not in _DEPENDENCY_AFFECTED_MODES:
            raise ValueError("unsupported deferred dependency affected mode")
        return value

    @model_validator(mode="after")
    def _dependency_valid(self) -> EvalModeDeferredDependency:
        if not self.dependency_id.strip() or not self.summary.strip():
            raise ValueError("deferred dependency fields must be non-empty")
        if (
            not self.static_descriptor_admission_behavior.strip()
            or not self.live_execution_behavior.strip()
        ):
            raise ValueError("deferred dependency behaviors must be non-empty")
        if self.affected_stage_id is None and not self.all_stage_scope:
            raise ValueError(
                "deferred dependency must declare stage or all-stage scope"
            )
        if self.affected_stage_id is not None and self.all_stage_scope:
            raise ValueError("deferred dependency cannot mix stage and all-stage scope")
        _reject_descriptor_material_leaks(self.model_dump(mode="json"))
        return self


class EvalModeConfound(BaseModel):
    """Structured comparability confound for eval mode reports."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    confound_id: StrictStr
    kind: StrictStr
    severity: StrictStr
    summary: StrictStr
    applies_to: tuple[StrictStr, ...]
    evidence: tuple[StrictStr, ...]
    comparison_effect: StrictStr
    mitigation: StrictStr

    @field_validator("kind")
    @classmethod
    def _kind_valid(cls, value: str) -> str:
        if value not in _CONFOUND_KINDS:
            raise ValueError("unsupported eval mode confound kind")
        return value

    @field_validator("severity")
    @classmethod
    def _severity_valid(cls, value: str) -> str:
        if value not in _CONFOUND_SEVERITIES:
            raise ValueError("unsupported eval mode confound severity")
        return value

    @model_validator(mode="after")
    def _confound_valid(self) -> EvalModeConfound:
        if not self.confound_id.strip() or not self.summary.strip():
            raise ValueError("confound fields must be non-empty")
        if not self.applies_to or any(not value.strip() for value in self.applies_to):
            raise ValueError("confound applies_to must be non-empty")
        if not self.evidence or any(not value.strip() for value in self.evidence):
            raise ValueError("confound evidence must be non-empty")
        if not self.comparison_effect.strip() or not self.mitigation.strip():
            raise ValueError(
                "confound comparison effect and mitigation must be non-empty"
            )
        _reject_descriptor_material_leaks(self.model_dump(mode="json"))
        return self


class EvalModeComparison(BaseModel):
    """One allowed or disallowed difference between two eval descriptors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: StrictStr
    summary: StrictStr
    left_value: Any
    right_value: Any

    @model_validator(mode="after")
    def _comparison_valid(self) -> EvalModeComparison:
        if not self.field_path.strip() or not self.summary.strip():
            raise ValueError("comparison fields must be non-empty")
        _reject_descriptor_material_leaks(self.model_dump(mode="json"))
        return self


class EvalModeFairnessReport(BaseModel):
    """Structured fairness comparison report for two eval mode descriptors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    left_mode_id: StrictStr
    right_mode_id: StrictStr
    comparable: StrictBool
    classification: StrictStr
    fairness_fingerprint_kind: StrictStr = EVAL_MODE_FAIRNESS_FINGERPRINT_KIND
    shared_fairness_fingerprint: StrictStr | None
    left_fairness_fingerprint: StrictStr
    right_fairness_fingerprint: StrictStr
    left_descriptor_fingerprint: StrictStr
    right_descriptor_fingerprint: StrictStr
    allowed_differences: tuple[EvalModeComparison, ...] = Field(default_factory=tuple)
    disallowed_differences: tuple[EvalModeComparison, ...] = Field(
        default_factory=tuple
    )
    confounds: tuple[EvalModeConfound, ...] = Field(default_factory=tuple)
    deferred_dependencies: tuple[EvalModeDeferredDependency, ...] = Field(
        default_factory=tuple
    )

    @model_validator(mode="after")
    def _report_valid(self) -> EvalModeFairnessReport:
        if self.fairness_fingerprint_kind != EVAL_MODE_FAIRNESS_FINGERPRINT_KIND:
            raise ValueError("unsupported fairness fingerprint kind")
        for fingerprint in (
            self.left_fairness_fingerprint,
            self.right_fairness_fingerprint,
            self.left_descriptor_fingerprint,
            self.right_descriptor_fingerprint,
        ):
            _validate_sha256(fingerprint)
        if self.shared_fairness_fingerprint is not None:
            _validate_sha256(self.shared_fairness_fingerprint)
        if self.comparable and self.disallowed_differences:
            raise ValueError(
                "comparable fairness reports cannot carry disallowed drift"
            )
        if self.classification != EVAL_COMPARISON_ENGINEERING_SMOKE_ONLY:
            raise ValueError("unsupported eval mode comparison classification")
        _reject_descriptor_material_leaks(self.model_dump(mode="json"))
        return self


class EvalModeLiveAdmissionResult(BaseModel):
    """Fail-closed live execution admission diagnostic for an eval mode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode_id: StrictStr
    admitted: StrictBool
    rule_id: StrictStr
    diagnostic_code: StrictStr | None = None
    diagnostic_summary: StrictStr | None = None
    deferred_dependencies: tuple[EvalModeDeferredDependency, ...] = Field(
        default_factory=tuple
    )

    @model_validator(mode="after")
    def _admission_valid(self) -> EvalModeLiveAdmissionResult:
        if self.admitted:
            if (
                self.diagnostic_code is not None
                or self.diagnostic_summary is not None
                or self.deferred_dependencies
            ):
                raise ValueError("admitted eval modes must not carry diagnostics")
        elif self.diagnostic_code is None or self.diagnostic_summary is None:
            raise ValueError("denied eval modes must include diagnostics")
        _reject_descriptor_material_leaks(self.model_dump(mode="json"))
        return self


class EvalModeDescriptor(BaseModel):
    """Immutable static descriptor for one compact eval mode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt = EVAL_MODE_SCHEMA_VERSION
    mode_id: StrictStr
    description: StrictStr
    runner_bindings: tuple[EvalRunnerBinding, ...]
    model_profile: EvalModelProfile
    graph_id: StrictStr
    graph_sha256: StrictStr
    stage_ids: tuple[EvalStageId, ...]
    stage_contracts: tuple[EvalStageContract, ...]
    terminal_results: tuple[EvalTerminalResult, ...]
    transition_semantics: tuple[Mapping[StrictStr, Any], ...]
    boundary_baseline: EvalBoundaryBaseline
    capability_envelopes: tuple[EvalCapabilityEnvelope, ...]
    fixture_policy: EvalFixtureWorkspacePolicy
    artifact_policy: tuple[EvalArtifactLayoutEntry, ...]
    context_tier: EvalContextTier
    stage_context_policies: tuple[EvalStageContextPolicy, ...]
    redaction_categories: tuple[StrictStr, ...]
    validator_visibility_policy: EvalValidatorVisibilityRecord
    trial_resource_ceiling: EvalResourceCeiling
    closure_boundary_id: StrictStr = EVAL_CLOSURE_BOUNDARY_ID
    deferred_dependencies: tuple[EvalModeDeferredDependency, ...] = Field(
        default_factory=tuple
    )
    declared_confounds: tuple[EvalModeConfound, ...] = Field(default_factory=tuple)
    fairness_fingerprint_kind: StrictStr = EVAL_MODE_FAIRNESS_FINGERPRINT_KIND
    fairness_fingerprint: StrictStr
    descriptor_fingerprint_kind: StrictStr = EVAL_MODE_FINGERPRINT_KIND
    descriptor_fingerprint: StrictStr

    @model_validator(mode="after")
    def _descriptor_valid(self) -> EvalModeDescriptor:
        if self.schema_version != EVAL_MODE_SCHEMA_VERSION:
            raise ValueError("unsupported eval mode schema_version")
        if self.mode_id not in {EVAL_SMALL_PI_MODE_ID, EVAL_SMALL_MILLFORGE_MODE_ID}:
            raise ValueError("unknown static eval mode id")
        if not self.description.strip():
            raise ValueError("descriptor description must be non-empty")
        graph = default_compact_eval_workflow_graph()
        workflow_snapshot = compact_eval_workflow_snapshot(graph)
        if self.graph_id != graph.graph_id:
            raise ValueError("descriptor graph_id does not match compact graph")
        if self.graph_sha256 != workflow_snapshot["graph_sha256"]:
            raise ValueError("descriptor graph_sha256 does not match compact graph")
        if self.stage_ids != graph.stage_ids:
            raise ValueError("descriptor stage_ids do not match compact graph")
        if self.stage_contracts != graph.stages:
            raise ValueError("descriptor stage contracts do not match compact graph")
        if self.terminal_results != tuple(EvalTerminalResult):
            raise ValueError("descriptor terminal results do not match compact graph")
        if self.transition_semantics != tuple(workflow_snapshot["transitions"]):
            raise ValueError("descriptor transitions do not match compact graph")
        if self.boundary_baseline != compact_eval_boundary_baseline():
            raise ValueError("descriptor boundary baseline does not match 06B")
        if self.capability_envelopes != tuple(
            default_eval_capability_envelopes()[stage_id] for stage_id in EvalStageId
        ):
            raise ValueError("descriptor capability envelopes do not match 06B")
        if self.fixture_policy != EvalFixtureWorkspacePolicy():
            raise ValueError("descriptor fixture policy does not match 06B")
        if self.artifact_policy != _default_artifact_policy_tuple():
            raise ValueError("descriptor artifact policy does not match 06B")
        if self.context_tier != EvalContextTier.COMPACT:
            raise ValueError("descriptor context tier does not match 06B")
        expected_context_policies = tuple(
            default_eval_stage_context_policies()[stage_id] for stage_id in EvalStageId
        )
        if self.stage_context_policies != expected_context_policies:
            raise ValueError("descriptor stage context policies do not match 06B")
        expected_redactions = expected_context_policies[0].redaction.categories
        if self.redaction_categories != expected_redactions:
            raise ValueError("descriptor redaction policy does not match 06B")
        if self.trial_resource_ceiling != default_eval_trial_resource_ceiling():
            raise ValueError("descriptor trial resource ceiling does not match 06B")
        if self.validator_visibility_policy != _default_validator_visibility_policy():
            raise ValueError(
                "descriptor validator visibility policy does not match 06B"
            )
        if self.closure_boundary_id != EVAL_CLOSURE_BOUNDARY_ID:
            raise ValueError("descriptor closure boundary does not match 06B")
        object.__setattr__(
            self,
            "transition_semantics",
            tuple(
                _freeze_descriptor_mapping(transition)
                for transition in self.transition_semantics
            ),
        )
        _validate_runner_bindings(self.mode_id, self.runner_bindings)
        if self.declared_confounds != _default_declared_confounds():
            raise ValueError("descriptor declared confounds do not match defaults")
        if self.fairness_fingerprint_kind != EVAL_MODE_FAIRNESS_FINGERPRINT_KIND:
            raise ValueError("unsupported fairness fingerprint kind")
        _validate_sha256(self.fairness_fingerprint)
        expected_fairness_fingerprint = calculate_eval_mode_fairness_fingerprint(self)
        if self.fairness_fingerprint != expected_fairness_fingerprint:
            raise ValueError("fairness_fingerprint does not match fairness payload")
        if self.descriptor_fingerprint_kind != EVAL_MODE_FINGERPRINT_KIND:
            raise ValueError("unsupported descriptor fingerprint kind")
        _validate_sha256(self.descriptor_fingerprint)
        expected_fingerprint = calculate_eval_mode_fingerprint(self)
        if self.descriptor_fingerprint != expected_fingerprint:
            raise ValueError("descriptor_fingerprint does not match descriptor payload")
        _reject_descriptor_material_leaks(self.model_dump(mode="json"))
        return self


def default_eval_model_profile() -> EvalModelProfile:
    """Return the shared backend-neutral model profile for static eval modes."""
    profile = EvalModelProfile.model_construct(
        profile_id=EVAL_DEFAULT_MODEL_PROFILE_ID,
        provider_label="provider-neutral",
        model_label="eval-small-backend-neutral",
        serving_class="local_openai_compatible",
        serving_protocol="openai_compatible_responses",
        tool_calling_mode="parser",
        parser_id="millforge.eval.parser.compact_json.v1",
        reasoning_effort="medium",
        temperature=0.0,
        top_p=1.0,
        max_prompt_tokens=32_000,
        max_completion_tokens=8_000,
        max_total_tokens=40_000,
        max_model_calls=2,
        cost_accounting={
            "currency": "none",
            "unit": "not_applicable",
            "rate_source": "static_descriptor",
        },
        model_profile_hash_kind=EVAL_MODEL_PROFILE_HASH_KIND,
        model_profile_hash="0" * 64,
    )
    return EvalModelProfile.model_validate(
        profile.model_copy(
            update={"model_profile_hash": calculate_eval_model_profile_hash(profile)}
        )
    )


def default_eval_small_pi_mode() -> EvalModeDescriptor:
    """Return the default compact eval descriptor for the Pi runner."""
    return _default_eval_mode(
        mode_id=EVAL_SMALL_PI_MODE_ID,
        runner_kind=EvalRunnerKind.PI,
    )


def default_eval_small_millforge_mode() -> EvalModeDescriptor:
    """Return the default compact eval descriptor for the Millforge runner."""
    return _default_eval_mode(
        mode_id=EVAL_SMALL_MILLFORGE_MODE_ID,
        runner_kind=EvalRunnerKind.MILLFORGE,
    )


def canonical_eval_mode_bytes(descriptor: EvalModeDescriptor) -> bytes:
    """Return canonical ASCII JSON bytes for a full eval mode descriptor."""
    return _canonical_eval_json_bytes(descriptor.model_dump(mode="json"))


def canonical_eval_mode_fairness_bytes(descriptor: EvalModeDescriptor) -> bytes:
    """Return canonical ASCII JSON bytes for the fairness-relevant projection."""
    return _canonical_eval_json_bytes(_fairness_payload(descriptor))


def calculate_eval_model_profile_hash(profile: EvalModelProfile) -> str:
    """Return the deterministic hash over backend-neutral model profile fields."""
    payload = profile.model_dump(mode="json")
    payload.pop("model_profile_hash", None)
    return hashlib.sha256(_canonical_eval_json_bytes(payload)).hexdigest()


def calculate_eval_mode_fingerprint(descriptor: EvalModeDescriptor) -> str:
    """Return the full deterministic descriptor fingerprint."""
    payload = descriptor.model_dump(mode="json")
    payload.pop("descriptor_fingerprint", None)
    payload.pop("fairness_fingerprint", None)
    return hashlib.sha256(_canonical_eval_json_bytes(payload)).hexdigest()


def calculate_eval_mode_fairness_fingerprint(
    descriptor: EvalModeDescriptor,
) -> str:
    """Return the deterministic descriptor fairness fingerprint."""
    return hashlib.sha256(canonical_eval_mode_fairness_bytes(descriptor)).hexdigest()


def compare_eval_modes_for_fairness(
    left: EvalModeDescriptor | None = None,
    right: EvalModeDescriptor | None = None,
) -> EvalModeFairnessReport:
    """Compare two eval mode descriptors for controlled fairness comparability."""
    left = left or default_eval_small_pi_mode()
    right = right or default_eval_small_millforge_mode()
    left_fairness = calculate_eval_mode_fairness_fingerprint(left)
    right_fairness = calculate_eval_mode_fairness_fingerprint(right)
    allowed = _allowed_runner_differences(left, right)
    disallowed = _disallowed_fairness_differences(left, right)
    deferred_dependencies = _unique_deferred_dependencies(
        left.deferred_dependencies + right.deferred_dependencies
    )
    confounds = _default_confounds_for_dependencies(deferred_dependencies)
    comparable = left_fairness == right_fairness and not disallowed
    return EvalModeFairnessReport(
        left_mode_id=left.mode_id,
        right_mode_id=right.mode_id,
        comparable=comparable,
        classification=EVAL_COMPARISON_ENGINEERING_SMOKE_ONLY,
        shared_fairness_fingerprint=left_fairness if comparable else None,
        left_fairness_fingerprint=left_fairness,
        right_fairness_fingerprint=right_fairness,
        left_descriptor_fingerprint=left.descriptor_fingerprint,
        right_descriptor_fingerprint=right.descriptor_fingerprint,
        allowed_differences=allowed,
        disallowed_differences=disallowed,
        confounds=confounds,
        deferred_dependencies=deferred_dependencies,
    )


def admit_eval_mode_live_execution(
    descriptor: EvalModeDescriptor,
    *,
    pi_live_runtime_available: bool = False,
    spec_07_harness_presets_available: bool = False,
    model_backend_configured: bool = False,
    resource_ceilings_enforceable: bool = False,
    fixture_workspace_creation_available: bool = False,
) -> EvalModeLiveAdmissionResult:
    """Return a fail-closed live execution admission result for an eval mode."""
    deferred = list(descriptor.deferred_dependencies)
    dependency_inputs = {
        "model_backend_configuration": model_backend_configured,
        "resource_ceiling_enforcement": resource_ceilings_enforceable,
        "fixture_workspace_creation": fixture_workspace_creation_available,
    }
    if any(
        binding.runner_kind == EvalRunnerKind.PI
        for binding in descriptor.runner_bindings
    ):
        dependency_inputs["pi_live_runtime_support"] = pi_live_runtime_available
    if any(
        binding.runner_kind == EvalRunnerKind.MILLFORGE
        for binding in descriptor.runner_bindings
    ):
        dependency_inputs["spec_07_harness_presets"] = spec_07_harness_presets_available

    existing_ids = {dependency.dependency_id for dependency in deferred}
    for dependency_id, available in dependency_inputs.items():
        if not available and dependency_id not in existing_ids:
            deferred.append(_deferred_dependency_for_id(dependency_id))
            existing_ids.add(dependency_id)

    unresolved = _unique_deferred_dependencies(tuple(deferred))
    if unresolved:
        return EvalModeLiveAdmissionResult(
            mode_id=descriptor.mode_id,
            admitted=False,
            rule_id="eval.mode.live_admission.deferred_dependency",
            diagnostic_code="MF-EVAL-M001",
            diagnostic_summary="live eval mode execution has unresolved dependencies",
            deferred_dependencies=unresolved,
        )
    return EvalModeLiveAdmissionResult(
        mode_id=descriptor.mode_id,
        admitted=True,
        rule_id="eval.mode.live_admission.allowed",
    )


def _default_eval_mode(
    *, mode_id: str, runner_kind: EvalRunnerKind
) -> EvalModeDescriptor:
    graph = default_compact_eval_workflow_graph()
    workflow_snapshot = compact_eval_workflow_snapshot(graph)
    context_policies = tuple(
        default_eval_stage_context_policies()[stage_id] for stage_id in EvalStageId
    )
    descriptor = EvalModeDescriptor.model_construct(
        schema_version=EVAL_MODE_SCHEMA_VERSION,
        mode_id=mode_id,
        description=_default_mode_description(mode_id),
        runner_bindings=tuple(
            EvalRunnerBinding(
                stage_id=stage_id,
                runner_kind=runner_kind,
                harness_id=(
                    EVAL_SPEC_07_HARNESS_IDS[stage_id]
                    if runner_kind == EvalRunnerKind.MILLFORGE
                    else None
                ),
            )
            for stage_id in graph.stage_ids
        ),
        model_profile=default_eval_model_profile(),
        graph_id=graph.graph_id,
        graph_sha256=workflow_snapshot["graph_sha256"],
        stage_ids=graph.stage_ids,
        stage_contracts=graph.stages,
        terminal_results=tuple(EvalTerminalResult),
        transition_semantics=tuple(workflow_snapshot["transitions"]),
        boundary_baseline=compact_eval_boundary_baseline(),
        capability_envelopes=tuple(
            default_eval_capability_envelopes()[stage_id] for stage_id in EvalStageId
        ),
        fixture_policy=EvalFixtureWorkspacePolicy(),
        artifact_policy=_default_artifact_policy_tuple(),
        context_tier=EvalContextTier.COMPACT,
        stage_context_policies=context_policies,
        redaction_categories=context_policies[0].redaction.categories,
        validator_visibility_policy=_default_validator_visibility_policy(),
        trial_resource_ceiling=default_eval_trial_resource_ceiling(),
        closure_boundary_id=EVAL_CLOSURE_BOUNDARY_ID,
        deferred_dependencies=(
            (
                _deferred_dependency_for_id("spec_07_harness_presets")
                if runner_kind == EvalRunnerKind.MILLFORGE
                else _deferred_dependency_for_id("pi_live_runtime_support")
            ),
        )
        if runner_kind in {EvalRunnerKind.MILLFORGE, EvalRunnerKind.PI}
        else (),
        declared_confounds=_default_declared_confounds(),
        fairness_fingerprint_kind=EVAL_MODE_FAIRNESS_FINGERPRINT_KIND,
        fairness_fingerprint="0" * 64,
        descriptor_fingerprint_kind=EVAL_MODE_FINGERPRINT_KIND,
        descriptor_fingerprint="0" * 64,
    )
    fairness_fingerprint = calculate_eval_mode_fairness_fingerprint(descriptor)
    descriptor = descriptor.model_copy(
        update={"fairness_fingerprint": fairness_fingerprint}
    )
    return EvalModeDescriptor.model_validate(
        descriptor.model_copy(
            update={
                "descriptor_fingerprint": calculate_eval_mode_fingerprint(descriptor)
            }
        )
    )


def _default_artifact_policy_tuple() -> tuple[EvalArtifactLayoutEntry, ...]:
    layout = canonical_eval_artifact_layout()
    return tuple(layout[artifact_id] for artifact_id in layout)


def _default_validator_visibility_policy() -> EvalValidatorVisibilityRecord:
    return EvalValidatorVisibilityRecord(
        visible_acceptance_check_ids=("public_acceptance_checks",)
    )


def _default_mode_description(mode_id: str) -> str:
    if mode_id == EVAL_SMALL_PI_MODE_ID:
        return (
            "Static compact eval descriptor for the Pi runner mode. The record "
            "admits descriptor validation only and defers live Pi runtime support."
        )
    if mode_id == EVAL_SMALL_MILLFORGE_MODE_ID:
        return (
            "Static compact eval descriptor for the Millforge runner mode. The "
            "record admits descriptor validation only and defers live Spec 07 "
            "harness execution."
        )
    raise ValueError("unknown static eval mode id")


def _fairness_payload(descriptor: EvalModeDescriptor) -> dict[str, Any]:
    return {
        "fairness_fingerprint_kind": EVAL_MODE_FAIRNESS_FINGERPRINT_KIND,
        "schema_version": descriptor.schema_version,
        "graph_id": descriptor.graph_id,
        "graph_sha256": descriptor.graph_sha256,
        "stage_ids": _json_value(descriptor.stage_ids),
        "terminal_results": _json_value(descriptor.terminal_results),
        "transition_semantics": _json_value(descriptor.transition_semantics),
        "attempt_limits": tuple(
            {
                "stage_id": contract.stage_id.value,
                "domain_attempt_limit": contract.domain_attempt_limit,
                "infrastructure_retry_limit": contract.infrastructure_retry_limit,
                "may_complete_workflow": contract.may_complete_workflow,
            }
            for contract in descriptor.stage_contracts
        ),
        "capability_envelopes": _json_value(descriptor.capability_envelopes),
        "fixture_policy": _json_value(descriptor.fixture_policy),
        "artifact_policy": _json_value(descriptor.artifact_policy),
        "context_tier": descriptor.context_tier.value,
        "stage_context_policies": _json_value(descriptor.stage_context_policies),
        "redaction_categories": _json_value(descriptor.redaction_categories),
        "validator_visibility_policy": _json_value(
            descriptor.validator_visibility_policy
        ),
        "visible_acceptance_check_policy": _json_value(
            descriptor.validator_visibility_policy.visible_acceptance_check_ids
        ),
        "trial_resource_ceiling": _json_value(descriptor.trial_resource_ceiling),
        "closure_boundary_id": descriptor.closure_boundary_id,
        "model_profile_hash": descriptor.model_profile.model_profile_hash,
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: _json_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(child) for child in value]
    return value


def _allowed_runner_differences(
    left: EvalModeDescriptor, right: EvalModeDescriptor
) -> tuple[EvalModeComparison, ...]:
    left_payload = left.model_dump(mode="json")
    right_payload = right.model_dump(mode="json")
    differences: list[EvalModeComparison] = []
    for field in _ALLOWED_RUNNER_DIFFERENCE_FIELDS:
        if left_payload[field] != right_payload[field]:
            differences.append(
                EvalModeComparison(
                    field_path=field,
                    summary=_allowed_difference_summary(field),
                    left_value=left_payload[field],
                    right_value=right_payload[field],
                )
            )
    return tuple(differences)


def _disallowed_fairness_differences(
    left: EvalModeDescriptor, right: EvalModeDescriptor
) -> tuple[EvalModeComparison, ...]:
    left_payload = _fairness_payload(left)
    right_payload = _fairness_payload(right)
    return tuple(
        EvalModeComparison(
            field_path=field,
            summary=f"fairness-critical {field} differs",
            left_value=left_payload[field],
            right_value=right_payload[field],
        )
        for field in left_payload
        if left_payload[field] != right_payload[field]
    )


def _allowed_difference_summary(field: str) -> str:
    if field == "runner_bindings":
        return (
            "runner kind, runner-specific binding ID, Millforge harness IDs, "
            "Pi adapter descriptor, runner-internal prompts or templates, and "
            "runner-internal tool-schema rendering are runner-specific"
        )
    if field == "deferred_dependencies":
        return "runner-specific live dependencies are reported as deferred diagnostics"
    return "mode identity is runner-specific"


def _default_confounds_for_dependencies(
    dependencies: tuple[EvalModeDeferredDependency, ...],
) -> tuple[EvalModeConfound, ...]:
    confounds: list[EvalModeConfound] = [
        _confound_record(
            confound_id="runner_capability_enforcement",
            kind="runner_capability_enforcement",
            severity="warning",
            summary="Pi and Millforge live runners may enforce capabilities differently",
            evidence=(
                "static descriptors bind different runner kinds",
                "capability envelopes are declared but no live runner is admitted",
            ),
            comparison_effect="live capability enforcement parity is unproven",
            mitigation="treat comparison as engineering smoke until live enforcement is measured",
        ),
        _confound_record(
            confound_id="tool_calling",
            kind="tool_calling",
            severity="warning",
            summary="runner tool-call behavior is not measured by static descriptors",
            evidence=(
                "model profile declares parser-mediated tool calling",
                "runner-internal tool schema rendering is runner-specific",
            ),
            comparison_effect="tool-call success and repair behavior may differ by runner",
            mitigation="compare live tool-call traces before controlled scoring",
        ),
        _confound_record(
            confound_id="parser_fallback",
            kind="parser_fallback",
            severity="info",
            summary="parser fallback behavior is backend neutral but unmeasured",
            evidence=(
                "model profile uses millforge.eval.parser.compact_json.v1",
                "no live malformed-output fallback evidence is present",
            ),
            comparison_effect="fallback frequency could affect live outcomes",
            mitigation="record parser fallback counts in live trial artifacts",
        ),
        _confound_record(
            confound_id="context_packing",
            kind="context_packing",
            severity="warning",
            summary="live context packing differences remain unmeasured",
            evidence=(
                "compact context policies are statically equal",
                "runner-specific prompt packing is outside the descriptor payload",
            ),
            comparison_effect="prompt shape parity is unproven at execution time",
            mitigation="capture and compare redacted context snapshots per stage",
        ),
        _confound_record(
            confound_id="model_endpoint",
            kind="model_endpoint",
            severity="invalidating",
            summary="no live model endpoint configuration is admitted",
            evidence=(
                "model backend configuration is a live deferred dependency",
                "descriptor contains no endpoint or private host material",
            ),
            comparison_effect="live score comparison is invalid without a shared endpoint",
            mitigation="admit live execution only after backend configuration is explicit",
        ),
        _confound_record(
            confound_id="token_accounting",
            kind="token_accounting",
            severity="warning",
            summary="live token accounting is not measured by static descriptors",
            evidence=(
                "model profile declares token ceilings",
                "no runtime token usage records are included in static descriptors",
            ),
            comparison_effect="cost and truncation behavior may differ in live runs",
            mitigation="compare model usage artifacts before controlled scoring",
        ),
        _confound_record(
            confound_id="wall_clock_measurement",
            kind="wall_clock_measurement",
            severity="info",
            summary="wall-clock timing is outside static descriptor comparison",
            evidence=(
                "resource ceilings declare wall-clock limits",
                "static descriptors contain no measured durations",
            ),
            comparison_effect="timing parity cannot be inferred from descriptors",
            mitigation="compare runtime resource usage artifacts for live trials",
        ),
    ]
    dependency_ids = {dependency.dependency_id for dependency in dependencies}
    if "spec_07_harness_presets" in dependency_ids:
        confounds.append(
            EvalModeConfound(
                confound_id="deferred_millforge_harness",
                kind="deferred_millforge_harness",
                severity="invalidating",
                summary="Spec 07 Millforge harness live execution is deferred",
                applies_to=(EVAL_SMALL_MILLFORGE_MODE_ID,),
                evidence=_spec_07_harness_readiness_evidence(),
                comparison_effect=(
                    "Millforge live readiness is blocked until harness execution "
                    "dependencies are admitted"
                ),
                mitigation=(
                    "admit Spec 07 harness execution dependencies before live runs"
                ),
            )
        )
    if "pi_live_runtime_support" in dependency_ids:
        confounds.append(
            EvalModeConfound(
                confound_id="deferred_pi_runtime",
                kind="deferred_pi_runtime",
                severity="invalidating",
                summary="Pi live runtime support is deferred",
                applies_to=(EVAL_SMALL_PI_MODE_ID,),
                evidence=("pi_live_runtime_support deferred dependency is unresolved",),
                comparison_effect="Pi live readiness is blocked by missing runtime support",
                mitigation="implement Pi live runtime support before live runs",
            )
        )
    return tuple(confounds)


def _default_declared_confounds() -> tuple[EvalModeConfound, ...]:
    return _default_confounds_for_dependencies(
        (
            _deferred_dependency_for_id("spec_07_harness_presets"),
            _deferred_dependency_for_id("pi_live_runtime_support"),
        )
    )


def _confound_record(
    *,
    confound_id: str,
    kind: str,
    severity: str,
    summary: str,
    evidence: tuple[str, ...],
    comparison_effect: str,
    mitigation: str,
) -> EvalModeConfound:
    return EvalModeConfound(
        confound_id=confound_id,
        kind=kind,
        severity=severity,
        summary=summary,
        applies_to=(EVAL_SMALL_PI_MODE_ID, EVAL_SMALL_MILLFORGE_MODE_ID),
        evidence=evidence,
        comparison_effect=comparison_effect,
        mitigation=mitigation,
    )


def _unique_deferred_dependencies(
    dependencies: tuple[EvalModeDeferredDependency, ...],
) -> tuple[EvalModeDeferredDependency, ...]:
    by_id: dict[str, EvalModeDeferredDependency] = {}
    for dependency in dependencies:
        by_id.setdefault(dependency.dependency_id, dependency)
    return tuple(by_id[dependency_id] for dependency_id in sorted(by_id))


def _deferred_dependency_for_id(dependency_id: str) -> EvalModeDeferredDependency:
    records = {
        "pi_live_runtime_support": {
            "dependency_kind": "runner_runtime",
            "affected_mode": EVAL_SMALL_PI_MODE_ID,
            "summary": "Pi live runtime support is not implemented",
            "reference_ids": (),
        },
        "spec_07_harness_presets": {
            "dependency_kind": "runner_harness",
            "affected_mode": EVAL_SMALL_MILLFORGE_MODE_ID,
            "summary": (
                "Spec 07 harness source records are available; live harness "
                "execution is not admitted"
            ),
            "reference_ids": _spec_07_implemented_harness_ids(),
        },
        "model_backend_configuration": {
            "dependency_kind": "model_backend",
            "affected_mode": "all_modes",
            "summary": "Live model backend configuration is missing",
            "reference_ids": (),
        },
        "resource_ceiling_enforcement": {
            "dependency_kind": "resource_enforcement",
            "affected_mode": "all_modes",
            "summary": "Resource ceilings cannot yet be enforced for live eval execution",
            "reference_ids": (),
        },
        "fixture_workspace_creation": {
            "dependency_kind": "fixture_workspace",
            "affected_mode": "all_modes",
            "summary": "Fixture workspace creation is unavailable",
            "reference_ids": (),
        },
    }
    if dependency_id not in _LIVE_DEPENDENCY_IDS:
        raise ValueError("unknown eval mode deferred dependency id")
    record = records[dependency_id]
    return EvalModeDeferredDependency(
        dependency_id=dependency_id,
        dependency_kind=str(record["dependency_kind"]),
        affected_mode=str(record["affected_mode"]),
        affected_stage_id=None,
        all_stage_scope=True,
        static_descriptor_admission_behavior="static descriptor validation passes",
        live_execution_behavior="live execution admission fails closed until resolved",
        summary=str(record["summary"]),
        reference_ids=tuple(record["reference_ids"]),
    )


def _spec_07_implemented_harness_ids() -> tuple[str, ...]:
    return tuple(
        EVAL_SPEC_07_HARNESS_IDS[stage_id]
        for stage_id in _EVAL_SPEC_07_IMPLEMENTED_HARNESS_STAGES
    )


def _spec_07_harness_readiness_evidence() -> tuple[str, ...]:
    planner_id, builder_id, checker_id, arbiter_id = _spec_07_implemented_harness_ids()
    return (
        f"Planner source record is implemented: {planner_id}",
        f"Builder source record is implemented: {builder_id}",
        f"Checker source record is implemented: {checker_id}",
        f"Arbiter source record is implemented: {arbiter_id}",
        "live Spec 07 harness execution remains unresolved",
    )


def _validate_runner_bindings(
    mode_id: str, runner_bindings: tuple[EvalRunnerBinding, ...]
) -> None:
    graph_stage_ids = default_compact_eval_workflow_graph().stage_ids
    if tuple(binding.stage_id for binding in runner_bindings) != graph_stage_ids:
        raise ValueError("runner bindings must cover exactly the compact eval stages")
    expected_kind = (
        EvalRunnerKind.PI
        if mode_id == EVAL_SMALL_PI_MODE_ID
        else EvalRunnerKind.MILLFORGE
    )
    for binding in runner_bindings:
        if binding.runner_kind != expected_kind:
            raise ValueError("runner binding kind does not match eval mode id")


def _canonical_eval_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).replace("\r\n", "\n")
        + "\n"
    ).encode("ascii")


def _freeze_descriptor_mapping(value: Mapping[Any, Any]) -> Mapping[Any, Any]:
    return _FrozenDescriptorDict(
        {key: _freeze_descriptor_value(child) for key, child in value.items()}
    )


def _freeze_descriptor_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_descriptor_mapping(value)
    if isinstance(value, tuple):
        return tuple(_freeze_descriptor_value(child) for child in value)
    if isinstance(value, list):
        return tuple(_freeze_descriptor_value(child) for child in value)
    return value


def _descriptor_text_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(
            text
            for item in value.items()
            for child in item
            for text in _descriptor_text_values(child)
        )
    if isinstance(value, (tuple, list, set, frozenset)):
        return tuple(text for child in value for text in _descriptor_text_values(child))
    return ()


def _reject_descriptor_material_leaks(value: Any) -> None:
    for text in _descriptor_text_values(value):
        lowered = text.lower()
        for token in _DENIED_DESCRIPTOR_TOKENS:
            if token in lowered:
                raise ValueError(
                    "eval mode descriptors must not expose private material"
                )
        if (
            _WINDOWS_ABSOLUTE_PATH.search(text)
            or _POSIX_ABSOLUTE_PATH.search(text)
            or _USER_HOME_PATH.search(text)
        ):
            raise ValueError("eval mode descriptors must not expose host paths")


def _validate_sha256(value: str) -> None:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("sha256 must be a lowercase hexadecimal SHA-256 digest")


__all__ = [
    "EVAL_DEFAULT_MODEL_PROFILE_ID",
    "EVAL_MODE_FINGERPRINT_KIND",
    "EVAL_MODE_FAIRNESS_FINGERPRINT_KIND",
    "EVAL_MODE_SCHEMA_VERSION",
    "EVAL_MODEL_PROFILE_HASH_KIND",
    "EVAL_SMALL_MILLFORGE_MODE_ID",
    "EVAL_SMALL_PI_MODE_ID",
    "EVAL_SPEC_07_HARNESS_IDS",
    "EvalModeComparison",
    "EvalModeConfound",
    "EvalModeDeferredDependency",
    "EvalModeDescriptor",
    "EvalModeFairnessReport",
    "EvalModeLiveAdmissionResult",
    "EvalModelProfile",
    "EvalRunnerBinding",
    "EvalRunnerKind",
    "admit_eval_mode_live_execution",
    "calculate_eval_mode_fairness_fingerprint",
    "calculate_eval_mode_fingerprint",
    "calculate_eval_model_profile_hash",
    "canonical_eval_mode_fairness_bytes",
    "canonical_eval_mode_bytes",
    "compare_eval_modes_for_fairness",
    "default_eval_model_profile",
    "default_eval_small_millforge_mode",
    "default_eval_small_pi_mode",
]
