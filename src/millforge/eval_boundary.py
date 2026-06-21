"""Public 06B baseline boundary anchored to the compact eval workflow."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from typing import Any

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

from millforge.eval_workflow import (
    EvalCandidateDisposition,
    EvalStageId,
    EvalTerminalResult,
    EvalWorkflowOutcomeKind,
    compact_eval_workflow_snapshot,
    default_compact_eval_workflow_graph,
)

AUTHORITATIVE_COMPACT_EVAL_WORKFLOW_NAMES: tuple[str, ...] = (
    "EvalStageId",
    "EvalTerminalResult",
    "EvalWorkflowOutcomeKind",
    "EvalCandidateDisposition",
    "default_compact_eval_workflow_graph",
    "compact_eval_workflow_snapshot",
)
EVAL_BOUNDARY_MODULE_NAME = "millforge.eval_boundary"
EVAL_BOUNDARY_ARTIFACT_MODULE_REQUIRED = True
EVAL_BOUNDARY_ARTIFACT_MODULE_DECISION = (
    "06B artifact schemas are defined in millforge.eval_artifacts so "
    "millforge.eval_boundary remains focused on capability and fixture policy."
)
EVAL_DENIED_CAPABILITY_IDS: tuple[str, ...] = (
    "network.access",
    "package.install",
    "git.mutate",
    "runtime.control",
)
EVAL_BUILDER_DEFAULT_WRITE_ROOTS: tuple[str, ...] = (
    "src",
    "tests",
    "README.md",
    "ROADMAP.md",
)
EVAL_CHECKER_IGNORED_SCRATCH_ROOTS: tuple[str, ...] = (
    ".eval-scratch",
    ".pytest_cache",
)
EVAL_FIXTURE_IGNORED_GENERATED_ROOTS: tuple[str, ...] = (
    ".eval-scratch",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
)
EVAL_FIXTURE_IGNORED_GENERATED_SUFFIXES: tuple[str, ...] = (
    ".coverage",
    ".coverage.json",
    ".log",
    ".pyc",
    ".pyo",
)
EVAL_FIXTURE_MANIFEST_SCHEMA_VERSION = 1
EVAL_FIXTURE_DEFAULT_REVISION = "fixture-revision-1"
EVAL_FIXTURE_FILE_ROLES: tuple[str, ...] = (
    "source",
    "test",
    "documentation",
    "configuration",
    "data",
)
EVAL_WORKSPACE_ISOLATION_CONTRACT = "fresh_copy"
EVAL_CONTEXT_FINGERPRINT_KIND = "eval_context_snapshot_sha256_v1"
EVAL_CONTEXT_DEFAULT_REDACTION_CATEGORIES: tuple[str, ...] = (
    "scorer_only_material",
    "secrets",
    "daemon_state",
    "host_paths",
    "runtime_private_state",
    "ideas_private_state",
    "reference_private_state",
    "git_history",
    "private_conversations",
    "unrelated_repository_outlines",
)
EVAL_CONTEXT_DEFAULT_REQUIRED_ARTIFACT_IDS: Mapping[EvalStageId, tuple[str, ...]] = (
    MappingProxyType(
        {
            EvalStageId.PLANNER: ("task", "fixture_manifest", "acceptance_checks"),
            EvalStageId.BUILDER: (
                "task",
                "fixture_manifest",
                "acceptance_checks",
                "plan",
            ),
            EvalStageId.CHECKER: (
                "fixture_manifest",
                "acceptance_checks",
                "workspace_diff",
                "patch_summary",
                "test_results",
            ),
            EvalStageId.ARBITER: (
                "acceptance_checks",
                "workspace_diff",
                "patch_summary",
                "test_results",
                "checker_verdict",
            ),
        }
    )
)
EVAL_CONTEXT_DEFAULT_ALLOWED_PATHS: Mapping[EvalStageId, tuple[str, ...]] = (
    MappingProxyType(
        {
            EvalStageId.PLANNER: (),
            EvalStageId.BUILDER: ("src", "tests", "README.md", "ROADMAP.md"),
            EvalStageId.CHECKER: ("src", "tests", ".eval-scratch"),
            EvalStageId.ARBITER: ("src", "tests"),
        }
    )
)
EVAL_STAGE_RESOURCE_CEILING_DEFAULTS: Mapping[EvalStageId, Mapping[str, int]] = (
    MappingProxyType(
        {
            EvalStageId.PLANNER: MappingProxyType(
                {
                    "prompt_tokens": 16_000,
                    "completion_tokens": 4_000,
                    "model_calls": 1,
                    "wall_clock_seconds": 600,
                    "shell_commands": 1,
                    "shell_command_seconds": 1,
                    "writable_bytes": 262_144,
                    "artifact_bytes": 262_144,
                }
            ),
            EvalStageId.BUILDER: MappingProxyType(
                {
                    "prompt_tokens": 32_000,
                    "completion_tokens": 8_000,
                    "model_calls": 2,
                    "wall_clock_seconds": 1_800,
                    "shell_commands": 24,
                    "shell_command_seconds": 900,
                    "writable_bytes": 2_097_152,
                    "artifact_bytes": 524_288,
                }
            ),
            EvalStageId.CHECKER: MappingProxyType(
                {
                    "prompt_tokens": 16_000,
                    "completion_tokens": 4_000,
                    "model_calls": 2,
                    "wall_clock_seconds": 900,
                    "shell_commands": 12,
                    "shell_command_seconds": 600,
                    "writable_bytes": 131_072,
                    "artifact_bytes": 524_288,
                }
            ),
            EvalStageId.ARBITER: MappingProxyType(
                {
                    "prompt_tokens": 16_000,
                    "completion_tokens": 4_000,
                    "model_calls": 1,
                    "wall_clock_seconds": 600,
                    "shell_commands": 1,
                    "shell_command_seconds": 1,
                    "writable_bytes": 131_072,
                    "artifact_bytes": 262_144,
                }
            ),
        }
    )
)
EVAL_TRIAL_RESOURCE_CEILING_DEFAULTS: Mapping[str, int] = MappingProxyType(
    {
        "prompt_tokens": 80_000,
        "completion_tokens": 20_000,
        "model_calls": 6,
        "wall_clock_seconds": 3_900,
        "shell_commands": 36,
        "shell_command_seconds": 1_500,
        "writable_bytes": 2_621_440,
        "artifact_bytes": 1_572_864,
    }
)
_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_SECRET_ENV_MARKERS: tuple[str, ...] = (
    "API_KEY",
    "AUTH",
    "CREDENTIAL",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)
_HIDDEN_CHECK_DENIED_TOKENS: tuple[str, ...] = (
    "definition",
    "expected",
    "fixture",
    "output",
    "path",
    "result",
    "rubric",
    "score",
    "secret",
)
_PACKAGE_MANAGER_COMMANDS: frozenset[str] = frozenset(
    {
        "cargo",
        "gem",
        "npm",
        "pip",
        "pip3",
        "pnpm",
        "poetry",
        "uv",
        "yarn",
    }
)
_NETWORK_COMMANDS: frozenset[str] = frozenset(
    {
        "curl",
        "ftp",
        "nc",
        "netcat",
        "scp",
        "sftp",
        "ssh",
        "telnet",
        "wget",
    }
)
_RUNTIME_CONTROL_COMMANDS: frozenset[str] = frozenset(
    {
        "docker",
        "docker-compose",
        "millrace",
        "podman",
        "service",
        "systemctl",
    }
)
_SHELL_COMMAND_WRAPPERS: frozenset[str] = frozenset(
    {
        "bash",
        "dash",
        "fish",
        "ksh",
        "sh",
        "zsh",
    }
)
_SHELL_INTERPOLATION_TOKENS: tuple[str, ...] = (
    "$",
    "`",
    "$(",
    "${",
    "{{",
    "}}",
    ";",
    "&&",
    "||",
    "|",
    "<",
    ">",
)
_CONTEXT_LEAK_TOKENS: tuple[str, ...] = (
    "F:\\",
    "/mnt/f",
    "millrace-agents",
    "ideas/",
    "ref-forge/",
    "/home/",
    "\\Users\\",
    "API_KEY",
    "DAEMON_STATE",
    "daemon state",
    "git history",
    "git_history",
    "hidden check",
    "hidden checks",
    "hidden_check",
    "hidden_checks",
    "hidden score",
    "hidden_score",
    "hidden_scores",
    "scoring rubric",
    "scoring_rubric",
    "expected output",
    "expected_output",
    "private conversation",
    "private conversations",
    "private_conversation",
    "private_conversations",
    "private runtime",
    "unrelated repository outline",
    "unrelated repository outlines",
    "unrelated_repository_outline",
    "unrelated_repository_outlines",
)
_CONTEXT_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s\"'`([{<])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'`)\]}>,]*"
)
_CONTEXT_POSIX_ABSOLUTE_PATH = re.compile(r"(?:^|[\s\"'`([{<])/(?!/)[^\s\"'`)\]}>,]*")
_CONTEXT_USER_HOME_PATH = re.compile(r"(?:^|[\s\"'`([{<])~(?:/|\\)[^\s\"'`)\]}>,]*")


def _command_executable(argv: tuple[str, ...]) -> str:
    return argv[0].split("/")[-1].lower()


def _is_python_executable(executable: str) -> bool:
    return executable in {"py", "python", "python3"} or executable.startswith(
        "python3."
    )


def _python_module_name(argv: tuple[str, ...], executable: str) -> str | None:
    if len(argv) < 3 or not _is_python_executable(executable) or argv[1] != "-m":
        return None
    return argv[2].split(".", maxsplit=1)[0].lower()


def _disallowed_argv_message(argv: tuple[str, ...]) -> str | None:
    executable = _command_executable(argv)
    if executable in _PACKAGE_MANAGER_COMMANDS:
        return "package manager commands are not admitted"
    if executable in _NETWORK_COMMANDS:
        return "network commands are not admitted"
    if executable in _RUNTIME_CONTROL_COMMANDS:
        return "runtime control commands are not admitted"
    if executable == "git":
        return "git commands are not admitted in eval command descriptors"
    if executable in _SHELL_COMMAND_WRAPPERS:
        return "shell wrapper commands are not admitted"
    module_name = _python_module_name(argv, executable)
    if module_name in _PACKAGE_MANAGER_COMMANDS:
        return "package manager module wrappers are not admitted"
    return None


class EvalCapabilityId(str, Enum):
    """Closed capability IDs admitted by compact eval policy."""

    ARTIFACT_READ = "artifact.read"
    ARTIFACT_WRITE = "artifact.write"
    EVIDENCE_EMIT = "evidence.emit"
    RUNNER_INVOKE = "runner.invoke"
    WORKSPACE_READ = "workspace.read"
    WORKSPACE_WRITE = "workspace.write"
    SHELL_RUN = "shell.run"
    NETWORK_ACCESS = "network.access"
    PACKAGE_INSTALL = "package.install"
    GIT_MUTATE = "git.mutate"
    RUNTIME_CONTROL = "runtime.control"


class EvalCapabilityEnvelope(BaseModel):
    """Immutable capability envelope for one compact eval stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_id: EvalStageId
    capability_ids: tuple[EvalCapabilityId, ...]

    @field_validator("capability_ids")
    @classmethod
    def _capability_ids_valid(
        cls, value: tuple[EvalCapabilityId, ...]
    ) -> tuple[EvalCapabilityId, ...]:
        if len(set(value)) != len(value):
            raise ValueError("capability_ids values must be unique")
        return value


class EvalCapabilityValidationResult(BaseModel):
    """Structured result for one stage capability admission decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_id: EvalStageId | StrictStr
    capability_id: EvalCapabilityId | StrictStr
    allowed: StrictBool
    rule_id: StrictStr
    diagnostic_code: StrictStr | None = None
    diagnostic_summary: StrictStr | None = None

    @model_validator(mode="after")
    def _diagnostic_shape_valid(self) -> EvalCapabilityValidationResult:
        if self.allowed:
            if self.diagnostic_code is not None or self.diagnostic_summary is not None:
                raise ValueError(
                    "allowed capability results must not include diagnostics"
                )
        elif self.diagnostic_code is None or self.diagnostic_summary is None:
            raise ValueError("denied capability results must include diagnostics")
        return self


class EvalCommandEnvironmentPolicy(BaseModel):
    """Closed environment policy for deterministic eval commands."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    inherit_environment: StrictBool = False
    variables: Mapping[StrictStr, StrictStr] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _environment_policy_valid(self) -> EvalCommandEnvironmentPolicy:
        if self.inherit_environment:
            raise ValueError("eval commands may not inherit ambient environment")
        ordered: dict[str, str] = {}
        for name, value in sorted(self.variables.items()):
            if not name or not name.replace("_", "").isalnum() or name[0].isdigit():
                raise ValueError(
                    "environment variable names must be stable identifiers"
                )
            if name == "*" or any(
                marker in name.upper() for marker in _SECRET_ENV_MARKERS
            ):
                raise ValueError("environment policy may not expose secrets")
            if "\x00" in value:
                raise ValueError(
                    "environment variable values must not contain NUL bytes"
                )
            ordered[name] = value
        object.__setattr__(self, "variables", MappingProxyType(ordered))
        return self


class EvalCommandDescriptor(BaseModel):
    """Deterministic descriptor for admitted Builder and Checker commands."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: StrictStr
    argv: tuple[StrictStr, ...]
    relative_working_directory: StrictStr = "."
    admitted_read_roots: tuple[StrictStr, ...]
    admitted_write_roots: tuple[StrictStr, ...] = Field(default_factory=tuple)
    timeout_seconds: StrictInt = Field(gt=0, le=600)
    environment_policy: EvalCommandEnvironmentPolicy = Field(
        default_factory=EvalCommandEnvironmentPolicy
    )
    expected_output_artifact_ids: tuple[StrictStr, ...]

    @field_validator("command_id")
    @classmethod
    def _command_id_valid(cls, value: str) -> str:
        if not value.strip() or any(
            token in value for token in _SHELL_INTERPOLATION_TOKENS
        ):
            raise ValueError("command_id must be a stable non-interpolated identifier")
        return value

    @field_validator("argv")
    @classmethod
    def _argv_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("argv must not be empty")
        if len(value) == 1 and any(character.isspace() for character in value[0]):
            raise ValueError("argv entries must not require shell interpolation")
        if disallowed_message := _disallowed_argv_message(value):
            raise ValueError(disallowed_message)
        for argument in value:
            if not argument or "\x00" in argument:
                raise ValueError("argv entries must be non-empty strings")
            if any(token in argument for token in _SHELL_INTERPOLATION_TOKENS):
                raise ValueError("argv entries must not require shell interpolation")
        return value

    @field_validator("admitted_read_roots", "admitted_write_roots")
    @classmethod
    def _roots_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("admitted roots must be unique")
        for root in value:
            _validate_relative_eval_path(root, allow_dot=False)
        return value

    @field_validator("expected_output_artifact_ids")
    @classmethod
    def _artifact_ids_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("expected_output_artifact_ids must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("expected_output_artifact_ids values must be unique")
        for artifact_id in value:
            if not artifact_id.strip() or "/" in artifact_id or "\\" in artifact_id:
                raise ValueError("expected output artifact ids must be stable IDs")
        return value

    @model_validator(mode="after")
    def _descriptor_shape_valid(self) -> EvalCommandDescriptor:
        _validate_relative_eval_path(self.relative_working_directory, allow_dot=True)
        if not self.admitted_read_roots:
            raise ValueError("admitted_read_roots must not be empty")
        if self.relative_working_directory != "." and not any(
            _path_is_within_root(self.relative_working_directory, root)
            for root in self.admitted_read_roots + self.admitted_write_roots
        ):
            raise ValueError("relative_working_directory must be inside admitted roots")
        return self


class EvalCommandAdmissionResult(BaseModel):
    """Structured command admission result for a compact eval stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_id: EvalStageId | StrictStr
    command_id: StrictStr
    allowed: StrictBool
    rule_id: StrictStr
    diagnostic_code: StrictStr | None = None
    diagnostic_summary: StrictStr | None = None

    @model_validator(mode="after")
    def _diagnostic_shape_valid(self) -> EvalCommandAdmissionResult:
        if self.allowed:
            if self.diagnostic_code is not None or self.diagnostic_summary is not None:
                raise ValueError("allowed command results must not include diagnostics")
        elif self.diagnostic_code is None or self.diagnostic_summary is None:
            raise ValueError("denied command results must include diagnostics")
        return self


class EvalBoundaryStageArtifacts(BaseModel):
    """Immutable logical artifact IDs for one compact eval stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_id: EvalStageId
    input_artifact_ids: tuple[StrictStr, ...]
    output_artifact_ids: tuple[StrictStr, ...]


class EvalBoundaryBaseline(BaseModel):
    """Immutable 06B boundary baseline derived from the accepted 06A graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt = 1
    module_name: StrictStr = EVAL_BOUNDARY_MODULE_NAME
    artifact_module_required: StrictBool = EVAL_BOUNDARY_ARTIFACT_MODULE_REQUIRED
    artifact_module_decision: StrictStr = EVAL_BOUNDARY_ARTIFACT_MODULE_DECISION
    authoritative_public_names: tuple[StrictStr, ...] = Field(
        default=AUTHORITATIVE_COMPACT_EVAL_WORKFLOW_NAMES
    )
    graph_id: StrictStr
    graph_sha256: StrictStr
    stage_ids: tuple[EvalStageId, ...]
    terminal_results: tuple[EvalTerminalResult, ...]
    outcome_kinds: tuple[EvalWorkflowOutcomeKind, ...]
    candidate_dispositions: tuple[EvalCandidateDisposition, ...]
    stage_artifacts: tuple[EvalBoundaryStageArtifacts, ...]


class EvalContextTier(str, Enum):
    """Closed context tiers for compact eval stage prompts."""

    COMPACT = "compact"
    VALIDATOR_VISIBLE = "validator_visible"


class EvalContextArtifactSummary(BaseModel):
    """Path-free summary of a model-visible artifact required by a stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: StrictStr
    summary: StrictStr

    @model_validator(mode="after")
    def _summary_valid(self) -> EvalContextArtifactSummary:
        _reject_context_material_leaks(self.model_dump(mode="json"))
        return self


class EvalContextRedaction(BaseModel):
    """Deterministic summary of material omitted from compact context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    categories: tuple[StrictStr, ...] = EVAL_CONTEXT_DEFAULT_REDACTION_CATEGORIES
    summary: StrictStr = "scorer-only and private workspace material omitted"
    redacted_item_count: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def _redaction_valid(self) -> EvalContextRedaction:
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("redaction categories must be unique")
        for category in self.categories:
            if not category.strip() or "/" in category or "\\" in category:
                raise ValueError("redaction categories must be stable identifiers")
        _reject_context_material_leaks(self.summary)
        return self


class EvalResourceCeiling(BaseModel):
    """Positive bounded resource ceiling for a compact eval trial or stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: StrictStr
    stage_id: EvalStageId | None = None
    prompt_tokens: StrictInt = Field(gt=0, le=1_000_000)
    completion_tokens: StrictInt = Field(gt=0, le=1_000_000)
    model_calls: StrictInt = Field(gt=0, le=100)
    wall_clock_seconds: StrictInt = Field(gt=0, le=86_400)
    shell_commands: StrictInt = Field(gt=0, le=1_000)
    shell_command_seconds: StrictInt = Field(gt=0, le=86_400)
    writable_bytes: StrictInt = Field(gt=0, le=1_073_741_824)
    artifact_bytes: StrictInt = Field(gt=0, le=1_073_741_824)

    @model_validator(mode="after")
    def _resource_ceiling_valid(self) -> EvalResourceCeiling:
        if self.scope not in {"trial", "stage"}:
            raise ValueError("resource ceiling scope must be trial or stage")
        if self.scope == "trial" and self.stage_id is not None:
            raise ValueError("trial resource ceilings must not declare stage_id")
        if self.scope == "stage" and self.stage_id is None:
            raise ValueError("stage resource ceilings must declare stage_id")
        return self


class EvalStageContextPolicy(BaseModel):
    """Compact context assembly policy for one 06A stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_id: EvalStageId
    context_tier: EvalContextTier = EvalContextTier.COMPACT
    allowed_capabilities: tuple[EvalCapabilityId, ...]
    allowed_paths: tuple[StrictStr, ...]
    required_artifact_ids: tuple[StrictStr, ...]
    include_current_stage_contract: StrictBool = True
    include_visible_acceptance_checks: StrictBool = True
    redaction: EvalContextRedaction = Field(
        default_factory=lambda: EvalContextRedaction(redacted_item_count=0)
    )
    resource_ceiling: EvalResourceCeiling

    @field_validator("allowed_paths")
    @classmethod
    def _allowed_paths_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("allowed_paths values must be unique")
        for path in value:
            _validate_relative_eval_path(path, allow_dot=False)
            _reject_context_material_leaks(path)
        return value

    @field_validator("required_artifact_ids")
    @classmethod
    def _required_artifacts_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("required_artifact_ids must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("required_artifact_ids values must be unique")
        for artifact_id in value:
            if not artifact_id.strip() or "/" in artifact_id or "\\" in artifact_id:
                raise ValueError("required artifact ids must be stable IDs")
        return value

    @model_validator(mode="after")
    def _context_policy_valid(self) -> EvalStageContextPolicy:
        envelope = _EVAL_CAPABILITY_ENVELOPES[self.stage_id]
        if self.allowed_capabilities != envelope.capability_ids:
            raise ValueError("context policy capabilities must match stage envelope")
        if self.resource_ceiling.scope != "stage":
            raise ValueError("stage context policies require a stage resource ceiling")
        if self.resource_ceiling.stage_id != self.stage_id:
            raise ValueError("resource ceiling stage_id must match context policy")
        return self


class EvalContextSnapshot(BaseModel):
    """Deterministic compact model-visible context snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_id: StrictStr
    stage_id: EvalStageId
    context_tier: EvalContextTier
    allowed_capabilities: tuple[StrictStr, ...]
    allowed_paths: tuple[StrictStr, ...]
    current_stage_contract: Mapping[StrictStr, Any]
    required_artifact_summaries: tuple[EvalContextArtifactSummary, ...]
    visible_acceptance_check_ids: tuple[StrictStr, ...]
    redaction: EvalContextRedaction
    byte_budget: StrictInt = Field(gt=0, le=1_073_741_824)
    token_budget: StrictInt = Field(gt=0, le=1_000_000)
    resource_ceiling: EvalResourceCeiling
    fingerprint_kind: StrictStr = EVAL_CONTEXT_FINGERPRINT_KIND
    fingerprint: StrictStr

    @field_validator("trial_id", "fingerprint")
    @classmethod
    def _snapshot_text_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("context snapshot text fields must be non-empty")
        _reject_context_material_leaks(value)
        return value

    @field_validator("allowed_paths")
    @classmethod
    def _snapshot_paths_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("allowed_paths values must be unique")
        for path in value:
            _validate_relative_eval_path(path, allow_dot=False)
            _reject_context_material_leaks(path)
        return value

    @field_validator("visible_acceptance_check_ids")
    @classmethod
    def _visible_check_ids_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("visible_acceptance_check_ids values must be unique")
        for check_id in value:
            if not check_id.strip() or "/" in check_id or "\\" in check_id:
                raise ValueError("visible acceptance check ids must be stable IDs")
            _reject_context_material_leaks(check_id)
        return value

    @model_validator(mode="after")
    def _snapshot_valid(self) -> EvalContextSnapshot:
        _validate_sha256(self.fingerprint)
        if self.fingerprint_kind != EVAL_CONTEXT_FINGERPRINT_KIND:
            raise ValueError("unsupported context fingerprint kind")
        if self.resource_ceiling.stage_id != self.stage_id:
            raise ValueError("context snapshot resource ceiling must match stage")
        _reject_context_material_leaks(self.model_dump(mode="json"))
        return self


class EvalPathPolicyViolation(BaseModel):
    """Structured relative-path policy diagnostic without host path leakage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: StrictStr
    rule_id: StrictStr
    diagnostic_code: StrictStr
    diagnostic_summary: StrictStr


class EvalFixtureFile(BaseModel):
    """Declared fixture file identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: StrictStr
    sha256: StrictStr
    size_bytes: StrictInt = Field(ge=0)
    role: StrictStr
    model_readable: StrictBool
    builder_mutable: StrictBool

    @field_validator("path")
    @classmethod
    def _path_valid(cls, value: str) -> str:
        _validate_relative_eval_path(value, allow_dot=False)
        return value

    @field_validator("role")
    @classmethod
    def _role_valid(cls, value: str) -> str:
        if value not in EVAL_FIXTURE_FILE_ROLES:
            raise ValueError("fixture file role is not in the closed role set")
        return value

    @field_validator("sha256")
    @classmethod
    def _sha256_valid(cls, value: str) -> str:
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("sha256 must be a lowercase hexadecimal SHA-256 digest")
        return value


class EvalFixtureWorkspacePolicy(BaseModel):
    """Workspace isolation and stage write policy for one eval fixture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_fixture_root_read_only: StrictBool = True
    workspace_isolation: StrictStr = EVAL_WORKSPACE_ISOLATION_CONTRACT
    stage_write_roots: Mapping[EvalStageId, tuple[StrictStr, ...]] = Field(
        default_factory=lambda: {
            EvalStageId.BUILDER: EVAL_BUILDER_DEFAULT_WRITE_ROOTS,
            EvalStageId.CHECKER: (),
            EvalStageId.ARBITER: (),
            EvalStageId.PLANNER: (),
        }
    )
    ignored_generated_roots: tuple[StrictStr, ...] = (
        EVAL_FIXTURE_IGNORED_GENERATED_ROOTS
    )
    ignored_generated_suffixes: tuple[StrictStr, ...] = (
        EVAL_FIXTURE_IGNORED_GENERATED_SUFFIXES
    )

    @model_validator(mode="after")
    def _policy_valid(self) -> EvalFixtureWorkspacePolicy:
        if not self.source_fixture_root_read_only:
            raise ValueError("source fixture root must be read-only")
        if self.workspace_isolation != EVAL_WORKSPACE_ISOLATION_CONTRACT:
            raise ValueError("fixture workspaces must use a fresh copied workspace")
        ordered_stage_write_roots: dict[EvalStageId, tuple[str, ...]] = {}
        for stage_id in EvalStageId:
            roots = tuple(self.stage_write_roots.get(stage_id, ()))
            if len(set(roots)) != len(roots):
                raise ValueError("stage write roots must be unique")
            for root in roots:
                _validate_relative_eval_path(root, allow_dot=False)
            ordered_stage_write_roots[stage_id] = roots
        if ordered_stage_write_roots[EvalStageId.CHECKER]:
            raise ValueError("checker fixture workspace policy must be read-only")
        if ordered_stage_write_roots[EvalStageId.ARBITER]:
            raise ValueError("arbiter fixture workspace policy must be read-only")
        if ordered_stage_write_roots[EvalStageId.PLANNER]:
            raise ValueError("planner fixture workspace policy must be read-only")
        for ignored_root in self.ignored_generated_roots:
            _validate_relative_eval_path(ignored_root, allow_dot=False)
        for suffix in self.ignored_generated_suffixes:
            if not suffix or "/" in suffix or "\\" in suffix:
                raise ValueError("ignored generated suffixes must be file suffixes")
        object.__setattr__(
            self, "stage_write_roots", MappingProxyType(ordered_stage_write_roots)
        )
        return self

    @field_serializer("stage_write_roots")
    def _serialize_stage_write_roots(
        self, value: Mapping[EvalStageId, tuple[str, ...]]
    ) -> dict[str, list[str]]:
        return {stage_id.value: list(value[stage_id]) for stage_id in EvalStageId}


class EvalFixtureManifest(BaseModel):
    """Deterministic fixture manifest with declared file hashes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: StrictInt
    fixture_id: StrictStr
    fixture_revision: StrictStr
    task_id: StrictStr
    source_root_label: StrictStr
    allowed_read_paths: tuple[StrictStr, ...]
    allowed_write_paths: tuple[StrictStr, ...]
    allowed_command_roots: tuple[StrictStr, ...]
    visible_acceptance_checks: tuple[StrictStr, ...]
    hidden_check_ids: tuple[StrictStr, ...]
    expected_mutation_paths: tuple[StrictStr, ...]
    files: tuple[EvalFixtureFile, ...]
    workspace_policy: EvalFixtureWorkspacePolicy = Field(
        default_factory=EvalFixtureWorkspacePolicy
    )

    @field_validator("schema_version")
    @classmethod
    def _schema_version_valid(cls, value: int) -> int:
        if value != EVAL_FIXTURE_MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported fixture manifest schema_version")
        return value

    @field_validator("fixture_id", "fixture_revision", "task_id", "source_root_label")
    @classmethod
    def _stable_text_valid(cls, value: str) -> str:
        if not value.strip() or "/" in value or "\\" in value:
            raise ValueError("fixture manifest text fields must be stable identifiers")
        _reject_context_material_leaks(value)
        return value

    @field_validator(
        "allowed_read_paths",
        "allowed_write_paths",
        "allowed_command_roots",
        "expected_mutation_paths",
    )
    @classmethod
    def _manifest_paths_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("fixture manifest paths must be unique")
        for path in value:
            try:
                _validate_relative_eval_path(path, allow_dot=False)
            except ValueError as exc:
                raise ValueError(
                    "fixture manifest paths must be normalized relative POSIX paths"
                ) from exc
            _reject_context_material_leaks(path)
        return tuple(sorted(value))

    @field_validator("visible_acceptance_checks")
    @classmethod
    def _visible_acceptance_checks_valid(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("visible acceptance checks must be unique")
        for check_id in value:
            _validate_opaque_eval_id(check_id, field_name="visible acceptance check")
        return value

    @field_validator("hidden_check_ids")
    @classmethod
    def _hidden_check_ids_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("hidden_check_ids must be unique")
        for check_id in value:
            _validate_opaque_eval_id(check_id, field_name="hidden check id")
            lowered = check_id.lower()
            if any(token in lowered for token in _HIDDEN_CHECK_DENIED_TOKENS):
                raise ValueError("hidden_check_ids must contain only opaque IDs")
        return value

    @model_validator(mode="after")
    def _manifest_valid(self) -> EvalFixtureManifest:
        paths = tuple(file.path for file in self.files)
        if not paths:
            raise ValueError("fixture manifests must declare at least one file")
        if len(set(paths)) != len(paths):
            raise ValueError("fixture file paths must be unique")
        for mutation_path in self.expected_mutation_paths:
            if mutation_path not in paths and not any(
                _path_is_within_root(path, mutation_path) for path in paths
            ):
                raise ValueError(
                    "expected_mutation_paths must reference declared fixture files or roots"
                )
        object.__setattr__(
            self, "files", tuple(sorted(self.files, key=lambda file: file.path))
        )
        return self


class EvalFixtureWorkspaceSnapshot(BaseModel):
    """Deterministic comparison between a declared manifest and workspace state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: StrictStr
    fixture_manifest_sha256: StrictStr
    files: tuple[EvalFixtureFile, ...]
    added_paths: tuple[StrictStr, ...] = Field(default_factory=tuple)
    modified_paths: tuple[StrictStr, ...] = Field(default_factory=tuple)
    deleted_paths: tuple[StrictStr, ...] = Field(default_factory=tuple)
    unchanged_paths: tuple[StrictStr, ...] = Field(default_factory=tuple)
    ignored_generated_paths: tuple[StrictStr, ...] = Field(default_factory=tuple)
    unauthorized_mutation_paths: tuple[StrictStr, ...] = Field(default_factory=tuple)
    violations: tuple[EvalPathPolicyViolation, ...] = Field(default_factory=tuple)

    @field_validator("fixture_manifest_sha256")
    @classmethod
    def _fixture_manifest_sha256_valid(cls, value: str) -> str:
        _validate_sha256(value)
        return value


class EvalClosureOutcomeKind(str, Enum):
    """Structured compact-eval closure validation outcomes."""

    VALID_CLOSED_SUCCESS = "valid_closed_success"
    VALID_CLOSED_REJECTION = "valid_closed_rejection"
    VALID_BLOCKED_OUTCOME = "valid_blocked_outcome"
    INVALID_ARTIFACT_BOUNDARY = "invalid_artifact_boundary"
    INVALID_CAPABILITY_BOUNDARY = "invalid_capability_boundary"
    INVALID_FIXTURE_BOUNDARY = "invalid_fixture_boundary"
    INVALID_CONTEXT_BOUNDARY = "invalid_context_boundary"


class EvalClosureValidationResult(BaseModel):
    """Non-mutating aggregate validation result for compact eval closure evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: StrictBool
    outcome_kind: EvalClosureOutcomeKind
    terminal_result: EvalTerminalResult | None = None
    candidate_disposition: EvalCandidateDisposition | None = None
    evidence_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    missing_artifact_ids: tuple[StrictStr, ...] = Field(default_factory=tuple)
    diagnostics: tuple[StrictStr, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _closure_result_valid(self) -> EvalClosureValidationResult:
        if self.valid:
            if self.outcome_kind not in {
                EvalClosureOutcomeKind.VALID_CLOSED_SUCCESS,
                EvalClosureOutcomeKind.VALID_CLOSED_REJECTION,
                EvalClosureOutcomeKind.VALID_BLOCKED_OUTCOME,
            }:
                raise ValueError("valid closure results require a valid outcome kind")
            if self.diagnostics:
                raise ValueError("valid closure results must not include diagnostics")
        elif self.outcome_kind in {
            EvalClosureOutcomeKind.VALID_CLOSED_SUCCESS,
            EvalClosureOutcomeKind.VALID_CLOSED_REJECTION,
            EvalClosureOutcomeKind.VALID_BLOCKED_OUTCOME,
        }:
            raise ValueError("invalid closure results require an invalid outcome kind")
        return self


def _validate_relative_eval_path(value: str, *, allow_dot: bool) -> None:
    if not value:
        raise ValueError("eval paths must be non-empty relative POSIX paths")
    if value == ".":
        if allow_dot:
            return
        raise ValueError("eval root paths must not be '.'")
    posix_path = PurePosixPath(value)
    parts = posix_path.parts
    if (
        value.startswith("/")
        or value.startswith("\\")
        or "\\" in value
        or "//" in value
        or value.startswith("../")
        or value.endswith("/..")
        or "/../" in value
        or value in {"..", ""}
        or _WINDOWS_DRIVE_PREFIX.match(value)
        or "." in parts
        or posix_path.as_posix() != value
    ):
        raise ValueError("eval paths must be normalized relative POSIX paths")


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


def _context_material_text_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(
            text
            for child in value.values()
            for text in _context_material_text_values(child)
        )
    if isinstance(value, (tuple, list, set, frozenset)):
        return tuple(
            text for child in value for text in _context_material_text_values(child)
        )
    return ()


def _reject_context_material_leaks(value: Any) -> None:
    for text in _context_material_text_values(value):
        if text in EVAL_CONTEXT_DEFAULT_REDACTION_CATEGORIES:
            continue
        lowered = text.lower()
        for token in _CONTEXT_LEAK_TOKENS:
            if token.lower() in lowered:
                raise ValueError(
                    "eval context snapshots must not expose private material"
                )
        if (
            _CONTEXT_WINDOWS_ABSOLUTE_PATH.search(text)
            or _CONTEXT_POSIX_ABSOLUTE_PATH.search(text)
            or _CONTEXT_USER_HOME_PATH.search(text)
        ):
            raise ValueError("eval context snapshots must not expose host paths")


def _validate_sha256(value: str) -> None:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("sha256 must be a lowercase hexadecimal SHA-256 digest")


def _validate_opaque_eval_id(value: str, *, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty opaque ID")
    if "/" in value or "\\" in value or _WINDOWS_DRIVE_PREFIX.match(value):
        raise ValueError(f"{field_name} must not contain paths")
    if any(character.isspace() for character in value):
        raise ValueError(f"{field_name} must not contain whitespace")
    _reject_context_material_leaks(value)


def _context_fingerprint_payload(snapshot: EvalContextSnapshot) -> dict[str, Any]:
    payload = snapshot.model_dump(mode="json")
    payload.pop("fingerprint", None)
    return payload


def calculate_eval_context_fingerprint(snapshot: EvalContextSnapshot) -> str:
    """Return the deterministic fingerprint for a compact context snapshot."""
    return hashlib.sha256(
        _canonical_eval_json_bytes(_context_fingerprint_payload(snapshot))
    ).hexdigest()


def _path_policy_violation(
    path: str,
    *,
    rule_id: str,
    diagnostic_code: str,
    diagnostic_summary: str,
) -> EvalPathPolicyViolation:
    return EvalPathPolicyViolation(
        path=_diagnostic_path(path),
        rule_id=rule_id,
        diagnostic_code=diagnostic_code,
        diagnostic_summary=diagnostic_summary,
    )


def _diagnostic_path(path: str) -> str:
    if (
        path.startswith("/")
        or path.startswith("\\")
        or _WINDOWS_DRIVE_PREFIX.match(path)
    ):
        return "<absolute-path>"
    return path


def validate_eval_fixture_path(
    path: str,
    *,
    filesystem_root: Path | None = None,
) -> EvalPathPolicyViolation | None:
    """Return a structured violation for an invalid fixture path, else None."""
    try:
        _validate_relative_eval_path(path, allow_dot=False)
    except ValueError:
        return _path_policy_violation(
            path=path,
            rule_id="eval.fixture.path.invalid",
            diagnostic_code="MF-EVAL-F001",
            diagnostic_summary="fixture paths must be normalized relative POSIX paths",
        )
    if filesystem_root is None:
        return None
    root = filesystem_root.resolve()
    candidate = (root / Path(*PurePosixPath(path).parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return _path_policy_violation(
            path=path,
            rule_id="eval.fixture.path.symlink_escape",
            diagnostic_code="MF-EVAL-F002",
            diagnostic_summary="fixture path resolves outside fixture root",
        )
    return None


def eval_fixture_file_hash(path: Path) -> str:
    """Return the SHA-256 hash for a fixture file."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_fixture_file_role(relative_path: str) -> str:
    if relative_path.startswith("tests/") or relative_path.startswith("test/"):
        return "test"
    if relative_path.startswith("src/"):
        return "source"
    if relative_path.endswith((".md", ".rst", ".txt")):
        return "documentation"
    if relative_path.endswith((".json", ".toml", ".yaml", ".yml", ".ini", ".cfg")):
        return "configuration"
    return "data"


def eval_fixture_file_record(
    fixture_root: Path,
    relative_path: str,
    *,
    role: str | None = None,
    model_readable: bool = True,
    builder_mutable: bool = False,
) -> EvalFixtureFile:
    """Build a deterministic file record for one relative fixture path."""
    violation = validate_eval_fixture_path(relative_path, filesystem_root=fixture_root)
    if violation is not None:
        raise ValueError(violation.diagnostic_summary)
    path = fixture_root / Path(*PurePosixPath(relative_path).parts)
    return EvalFixtureFile(
        path=relative_path,
        sha256=eval_fixture_file_hash(path),
        size_bytes=path.stat().st_size,
        role=role or _default_fixture_file_role(relative_path),
        model_readable=model_readable,
        builder_mutable=builder_mutable,
    )


def eval_fixture_manifest_sha256(manifest: EvalFixtureManifest) -> str:
    """Return the deterministic SHA-256 of an expanded fixture manifest payload."""
    return hashlib.sha256(
        _canonical_eval_json_bytes(manifest.model_dump(mode="json"))
    ).hexdigest()


def eval_fixture_manifest_from_paths(
    fixture_id: str,
    fixture_root: Path,
    relative_paths: tuple[str, ...],
    *,
    task_id: str,
    schema_version: int = EVAL_FIXTURE_MANIFEST_SCHEMA_VERSION,
    fixture_revision: str = EVAL_FIXTURE_DEFAULT_REVISION,
    source_root_label: str = "fixture_workspace",
    allowed_read_paths: tuple[str, ...] | None = None,
    allowed_write_paths: tuple[str, ...] = EVAL_BUILDER_DEFAULT_WRITE_ROOTS,
    allowed_command_roots: tuple[str, ...] = ("src", "tests"),
    visible_acceptance_checks: tuple[str, ...] = (),
    hidden_check_ids: tuple[str, ...] = (),
    expected_mutation_paths: tuple[str, ...] | None = None,
    workspace_policy: EvalFixtureWorkspacePolicy | None = None,
) -> EvalFixtureManifest:
    """Build a deterministic manifest from declared fixture-relative files."""
    declared_paths = tuple(sorted(relative_paths))
    mutation_paths = expected_mutation_paths or ()
    return EvalFixtureManifest(
        schema_version=schema_version,
        fixture_id=fixture_id,
        fixture_revision=fixture_revision,
        task_id=task_id,
        source_root_label=source_root_label,
        allowed_read_paths=allowed_read_paths or declared_paths,
        allowed_write_paths=allowed_write_paths,
        allowed_command_roots=allowed_command_roots,
        visible_acceptance_checks=visible_acceptance_checks,
        hidden_check_ids=hidden_check_ids,
        expected_mutation_paths=mutation_paths,
        files=tuple(
            eval_fixture_file_record(
                fixture_root,
                relative_path,
                builder_mutable=relative_path in mutation_paths,
            )
            for relative_path in declared_paths
        ),
        workspace_policy=workspace_policy or EvalFixtureWorkspacePolicy(),
    )


def _is_ignored_generated_path(path: str, policy: EvalFixtureWorkspacePolicy) -> bool:
    parts = PurePosixPath(path).parts
    return any(
        root in parts or _path_is_within_root(path, root)
        for root in policy.ignored_generated_roots
    ) or any(path.endswith(suffix) for suffix in policy.ignored_generated_suffixes)


def _workspace_files(
    workspace_root: Path,
) -> tuple[tuple[str, ...], tuple[EvalPathPolicyViolation, ...]]:
    paths: list[str] = []
    violations: list[EvalPathPolicyViolation] = []
    for file_path in workspace_root.rglob("*"):
        if file_path.is_file():
            relative_path = file_path.relative_to(workspace_root).as_posix()
            violation = validate_eval_fixture_path(
                relative_path, filesystem_root=workspace_root
            )
            if violation is None:
                paths.append(relative_path)
            else:
                violations.append(violation)
    return tuple(sorted(paths)), tuple(sorted(violations, key=lambda item: item.path))


def _unauthorized_paths(
    *,
    stage_id: EvalStageId,
    added_paths: tuple[str, ...],
    modified_paths: tuple[str, ...],
    deleted_paths: tuple[str, ...],
    policy: EvalFixtureWorkspacePolicy,
) -> tuple[str, ...]:
    allowed_roots = policy.stage_write_roots.get(stage_id, ())
    changed_paths = added_paths + modified_paths + deleted_paths
    if not allowed_roots:
        return tuple(sorted(changed_paths))
    return tuple(
        sorted(
            path
            for path in changed_paths
            if not any(_path_is_within_root(path, root) for root in allowed_roots)
        )
    )


def eval_fixture_workspace_snapshot(
    manifest: EvalFixtureManifest,
    workspace_root: Path,
    *,
    stage_id: EvalStageId = EvalStageId.BUILDER,
) -> EvalFixtureWorkspaceSnapshot:
    """Compare a workspace with its fixture manifest using relative paths only."""
    declared = {file.path: file for file in manifest.files}
    current_files: dict[str, EvalFixtureFile] = {}
    ignored_generated_paths: list[str] = []
    workspace_files, workspace_violations = _workspace_files(workspace_root)
    violations: list[EvalPathPolicyViolation] = list(workspace_violations)
    for relative_path in workspace_files:
        if _is_ignored_generated_path(relative_path, manifest.workspace_policy):
            ignored_generated_paths.append(relative_path)
            continue
        current_files[relative_path] = eval_fixture_file_record(
            workspace_root, relative_path
        )

    declared_paths = set(declared)
    current_paths = set(current_files)
    added_paths = tuple(sorted(current_paths - declared_paths))
    deleted_paths = tuple(sorted(declared_paths - current_paths))
    unchanged_paths = tuple(
        sorted(
            path
            for path in declared_paths & current_paths
            if declared[path].sha256 == current_files[path].sha256
            and declared[path].size_bytes == current_files[path].size_bytes
        )
    )
    modified_paths = tuple(
        sorted((declared_paths & current_paths) - set(unchanged_paths))
    )
    unauthorized_mutation_paths = _unauthorized_paths(
        stage_id=stage_id,
        added_paths=added_paths,
        modified_paths=modified_paths,
        deleted_paths=deleted_paths,
        policy=manifest.workspace_policy,
    )
    unauthorized_mutation_paths = tuple(
        sorted(
            set(unauthorized_mutation_paths)
            | {violation.path for violation in violations}
        )
    )
    return EvalFixtureWorkspaceSnapshot(
        fixture_id=manifest.fixture_id,
        fixture_manifest_sha256=eval_fixture_manifest_sha256(manifest),
        files=tuple(current_files[path] for path in sorted(current_files)),
        added_paths=added_paths,
        modified_paths=modified_paths,
        deleted_paths=deleted_paths,
        unchanged_paths=unchanged_paths,
        ignored_generated_paths=tuple(sorted(ignored_generated_paths)),
        unauthorized_mutation_paths=unauthorized_mutation_paths,
        violations=tuple(violations),
    )


def _path_is_within_root(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def _coerce_stage_id(stage_id: EvalStageId | str) -> EvalStageId | str:
    if isinstance(stage_id, EvalStageId):
        return stage_id
    try:
        return EvalStageId(stage_id)
    except ValueError:
        return stage_id


def _coerce_capability_id(capability: EvalCapabilityId | str) -> EvalCapabilityId | str:
    if isinstance(capability, EvalCapabilityId):
        return capability
    try:
        return EvalCapabilityId(capability)
    except ValueError:
        return capability


_EVAL_CAPABILITY_ENVELOPES: Mapping[EvalStageId, EvalCapabilityEnvelope] = (
    MappingProxyType(
        {
            EvalStageId.PLANNER: EvalCapabilityEnvelope(
                stage_id=EvalStageId.PLANNER,
                capability_ids=(
                    EvalCapabilityId.ARTIFACT_READ,
                    EvalCapabilityId.ARTIFACT_WRITE,
                    EvalCapabilityId.EVIDENCE_EMIT,
                    EvalCapabilityId.RUNNER_INVOKE,
                ),
            ),
            EvalStageId.BUILDER: EvalCapabilityEnvelope(
                stage_id=EvalStageId.BUILDER,
                capability_ids=(
                    EvalCapabilityId.ARTIFACT_READ,
                    EvalCapabilityId.ARTIFACT_WRITE,
                    EvalCapabilityId.EVIDENCE_EMIT,
                    EvalCapabilityId.RUNNER_INVOKE,
                    EvalCapabilityId.WORKSPACE_READ,
                    EvalCapabilityId.WORKSPACE_WRITE,
                    EvalCapabilityId.SHELL_RUN,
                ),
            ),
            EvalStageId.CHECKER: EvalCapabilityEnvelope(
                stage_id=EvalStageId.CHECKER,
                capability_ids=(
                    EvalCapabilityId.WORKSPACE_READ,
                    EvalCapabilityId.ARTIFACT_READ,
                    EvalCapabilityId.ARTIFACT_WRITE,
                    EvalCapabilityId.SHELL_RUN,
                    EvalCapabilityId.EVIDENCE_EMIT,
                    EvalCapabilityId.RUNNER_INVOKE,
                ),
            ),
            EvalStageId.ARBITER: EvalCapabilityEnvelope(
                stage_id=EvalStageId.ARBITER,
                capability_ids=(
                    EvalCapabilityId.WORKSPACE_READ,
                    EvalCapabilityId.ARTIFACT_READ,
                    EvalCapabilityId.ARTIFACT_WRITE,
                    EvalCapabilityId.EVIDENCE_EMIT,
                    EvalCapabilityId.RUNNER_INVOKE,
                ),
            ),
        }
    )
)


def default_eval_stage_resource_ceiling(stage_id: EvalStageId) -> EvalResourceCeiling:
    """Return the default positive bounded resource ceiling for one stage."""
    return EvalResourceCeiling(
        scope="stage",
        stage_id=stage_id,
        **dict(EVAL_STAGE_RESOURCE_CEILING_DEFAULTS[stage_id]),
    )


def default_eval_trial_resource_ceiling() -> EvalResourceCeiling:
    """Return the default positive bounded resource ceiling for one trial."""
    return EvalResourceCeiling(
        scope="trial", **dict(EVAL_TRIAL_RESOURCE_CEILING_DEFAULTS)
    )


def default_eval_stage_context_policy(
    stage_id: EvalStageId,
    *,
    context_tier: EvalContextTier = EvalContextTier.COMPACT,
) -> EvalStageContextPolicy:
    """Return the default compact context policy for one 06A stage."""
    return EvalStageContextPolicy(
        stage_id=stage_id,
        context_tier=context_tier,
        allowed_capabilities=_EVAL_CAPABILITY_ENVELOPES[stage_id].capability_ids,
        allowed_paths=EVAL_CONTEXT_DEFAULT_ALLOWED_PATHS[stage_id],
        required_artifact_ids=EVAL_CONTEXT_DEFAULT_REQUIRED_ARTIFACT_IDS[stage_id],
        resource_ceiling=default_eval_stage_resource_ceiling(stage_id),
    )


def default_eval_stage_context_policies() -> Mapping[
    EvalStageId, EvalStageContextPolicy
]:
    """Return immutable compact context policies keyed by 06A stage ID."""
    return MappingProxyType(
        {
            stage_id: default_eval_stage_context_policy(stage_id)
            for stage_id in EvalStageId
        }
    )


def build_eval_context_snapshot(
    *,
    trial_id: str,
    policy: EvalStageContextPolicy,
    required_artifact_summaries: tuple[EvalContextArtifactSummary, ...],
    visible_acceptance_check_ids: tuple[str, ...],
    byte_budget: int | None = None,
    token_budget: int | None = None,
) -> EvalContextSnapshot:
    """Build a deterministic compact model-visible context snapshot."""
    graph = default_compact_eval_workflow_graph()
    stage_contract = next(
        stage for stage in graph.stages if stage.stage_id == policy.stage_id
    )
    snapshot = EvalContextSnapshot(
        trial_id=trial_id,
        stage_id=policy.stage_id,
        context_tier=policy.context_tier,
        allowed_capabilities=tuple(
            capability.value for capability in policy.allowed_capabilities
        ),
        allowed_paths=policy.allowed_paths,
        current_stage_contract=stage_contract.model_dump(mode="json"),
        required_artifact_summaries=required_artifact_summaries,
        visible_acceptance_check_ids=visible_acceptance_check_ids,
        redaction=policy.redaction,
        byte_budget=byte_budget or policy.resource_ceiling.artifact_bytes,
        token_budget=token_budget or policy.resource_ceiling.prompt_tokens,
        resource_ceiling=policy.resource_ceiling,
        fingerprint="0" * 64,
    )
    return snapshot.model_copy(
        update={"fingerprint": calculate_eval_context_fingerprint(snapshot)}
    )


def compact_eval_boundary_baseline() -> EvalBoundaryBaseline:
    """Return the 06B module-shape baseline without rewriting 06A graph rules."""
    graph = default_compact_eval_workflow_graph()
    snapshot = compact_eval_workflow_snapshot(graph)
    return EvalBoundaryBaseline(
        graph_id=graph.graph_id,
        graph_sha256=snapshot["graph_sha256"],
        stage_ids=graph.stage_ids,
        terminal_results=tuple(EvalTerminalResult),
        outcome_kinds=tuple(EvalWorkflowOutcomeKind),
        candidate_dispositions=tuple(EvalCandidateDisposition),
        stage_artifacts=tuple(
            EvalBoundaryStageArtifacts(
                stage_id=stage.stage_id,
                input_artifact_ids=stage.input_artifact_ids,
                output_artifact_ids=stage.output_artifact_ids,
            )
            for stage in graph.stages
        ),
    )


def compact_eval_boundary_baseline_snapshot() -> dict[str, Any]:
    """Return a deterministic JSON-compatible baseline snapshot."""
    return compact_eval_boundary_baseline().model_dump(mode="json")


def default_eval_capability_envelopes() -> Mapping[EvalStageId, EvalCapabilityEnvelope]:
    """Return immutable compact eval capability envelopes keyed by 06A stage ID."""
    return _EVAL_CAPABILITY_ENVELOPES


def eval_stage_capability_envelope(stage_id: EvalStageId) -> EvalCapabilityEnvelope:
    """Return the immutable capability envelope for one compact eval stage."""
    return _EVAL_CAPABILITY_ENVELOPES[stage_id]


def validate_eval_stage_capability(
    stage_id: EvalStageId | str, capability: EvalCapabilityId | str
) -> EvalCapabilityValidationResult:
    """Validate one capability request against deterministic compact eval policy."""
    resolved_stage_id = _coerce_stage_id(stage_id)
    resolved_capability = _coerce_capability_id(capability)
    if not isinstance(resolved_stage_id, EvalStageId):
        return EvalCapabilityValidationResult(
            stage_id=resolved_stage_id,
            capability_id=resolved_capability,
            allowed=False,
            rule_id="eval.capability.unknown_stage",
            diagnostic_code="MF-EVAL-C001",
            diagnostic_summary="unknown compact eval stage id",
        )
    if not isinstance(resolved_capability, EvalCapabilityId):
        return EvalCapabilityValidationResult(
            stage_id=resolved_stage_id,
            capability_id=resolved_capability,
            allowed=False,
            rule_id="eval.capability.unknown_capability",
            diagnostic_code="MF-EVAL-C002",
            diagnostic_summary="unknown compact eval capability id",
        )
    envelope = _EVAL_CAPABILITY_ENVELOPES[resolved_stage_id]
    if resolved_capability in envelope.capability_ids:
        return EvalCapabilityValidationResult(
            stage_id=resolved_stage_id,
            capability_id=resolved_capability,
            allowed=True,
            rule_id="eval.capability.allowed",
        )
    if resolved_capability.value in EVAL_DENIED_CAPABILITY_IDS:
        return EvalCapabilityValidationResult(
            stage_id=resolved_stage_id,
            capability_id=resolved_capability,
            allowed=False,
            rule_id="eval.capability.denied_dangerous_all_stages",
            diagnostic_code="MF-EVAL-C003",
            diagnostic_summary="capability is denied for every compact eval stage",
        )
    return EvalCapabilityValidationResult(
        stage_id=resolved_stage_id,
        capability_id=resolved_capability,
        allowed=False,
        rule_id=f"eval.capability.denied.{resolved_stage_id.value}",
        diagnostic_code="MF-EVAL-C004",
        diagnostic_summary="capability is not in the stage envelope",
    )


def validate_eval_stage_command(
    stage_id: EvalStageId | str,
    descriptor: EvalCommandDescriptor,
    *,
    builder_allowed_write_roots: tuple[str, ...] = EVAL_BUILDER_DEFAULT_WRITE_ROOTS,
) -> EvalCommandAdmissionResult:
    """Validate a deterministic command descriptor for one compact eval stage."""
    resolved_stage_id = _coerce_stage_id(stage_id)
    if not isinstance(resolved_stage_id, EvalStageId):
        return EvalCommandAdmissionResult(
            stage_id=resolved_stage_id,
            command_id=descriptor.command_id,
            allowed=False,
            rule_id="eval.command.unknown_stage",
            diagnostic_code="MF-EVAL-D001",
            diagnostic_summary="unknown compact eval stage id",
        )
    shell_result = validate_eval_stage_capability(
        resolved_stage_id, EvalCapabilityId.SHELL_RUN
    )
    if not shell_result.allowed:
        return EvalCommandAdmissionResult(
            stage_id=resolved_stage_id,
            command_id=descriptor.command_id,
            allowed=False,
            rule_id="eval.command.stage_has_no_shell",
            diagnostic_code="MF-EVAL-D002",
            diagnostic_summary="stage cannot run shell commands",
        )
    if disallowed_message := _disallowed_argv_message(descriptor.argv):
        return EvalCommandAdmissionResult(
            stage_id=resolved_stage_id,
            command_id=descriptor.command_id,
            allowed=False,
            rule_id="eval.command.descriptor_unsafe",
            diagnostic_code="MF-EVAL-D005",
            diagnostic_summary=disallowed_message,
        )
    if resolved_stage_id == EvalStageId.CHECKER:
        invalid_checker_writes = tuple(
            root
            for root in descriptor.admitted_write_roots
            if not any(
                _path_is_within_root(root, scratch)
                for scratch in EVAL_CHECKER_IGNORED_SCRATCH_ROOTS
            )
        )
        if invalid_checker_writes:
            return EvalCommandAdmissionResult(
                stage_id=resolved_stage_id,
                command_id=descriptor.command_id,
                allowed=False,
                rule_id="eval.command.checker_write_denied",
                diagnostic_code="MF-EVAL-D003",
                diagnostic_summary="checker commands may write only ignored scratch outputs",
            )
    if resolved_stage_id == EvalStageId.BUILDER:
        for root in builder_allowed_write_roots:
            _validate_relative_eval_path(root, allow_dot=False)
        invalid_builder_writes = tuple(
            root
            for root in descriptor.admitted_write_roots
            if not any(
                _path_is_within_root(root, allowed_root)
                for allowed_root in builder_allowed_write_roots
            )
        )
        if invalid_builder_writes:
            return EvalCommandAdmissionResult(
                stage_id=resolved_stage_id,
                command_id=descriptor.command_id,
                allowed=False,
                rule_id="eval.command.builder_write_root_denied",
                diagnostic_code="MF-EVAL-D004",
                diagnostic_summary="builder command writes outside allowed roots",
            )
    return EvalCommandAdmissionResult(
        stage_id=resolved_stage_id,
        command_id=descriptor.command_id,
        allowed=True,
        rule_id="eval.command.allowed",
    )


def _closure_result(
    outcome_kind: EvalClosureOutcomeKind,
    *,
    terminal_result: EvalTerminalResult | None = None,
    candidate_disposition: EvalCandidateDisposition | None = None,
    evidence_artifact_ids: tuple[str, ...] = (),
    missing_artifact_ids: tuple[str, ...] = (),
    diagnostics: tuple[str, ...] = (),
) -> EvalClosureValidationResult:
    return EvalClosureValidationResult(
        valid=outcome_kind
        in {
            EvalClosureOutcomeKind.VALID_CLOSED_SUCCESS,
            EvalClosureOutcomeKind.VALID_CLOSED_REJECTION,
            EvalClosureOutcomeKind.VALID_BLOCKED_OUTCOME,
        },
        outcome_kind=outcome_kind,
        terminal_result=terminal_result,
        candidate_disposition=candidate_disposition,
        evidence_artifact_ids=evidence_artifact_ids,
        missing_artifact_ids=missing_artifact_ids,
        diagnostics=diagnostics,
    )


def _artifact_bundle_items(
    artifact_bundle: Mapping[Any, Any],
) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for raw_artifact_id, record in artifact_bundle.items():
        artifact_id = getattr(raw_artifact_id, "value", raw_artifact_id)
        artifacts[str(artifact_id)] = record
    return artifacts


def _artifact_record_mapping(record: Any) -> Mapping[str, Any]:
    if isinstance(record, BaseModel):
        return record.model_dump(mode="json")
    if isinstance(record, Mapping):
        return record
    raise TypeError("artifact records must be mappings or Pydantic models")


def _fixture_manifest_from_artifact_record(record: Any) -> EvalFixtureManifest:
    from millforge.eval_artifacts import EvalFixtureManifestArtifact

    if isinstance(record, EvalFixtureManifest):
        return record
    if isinstance(record, EvalFixtureManifestArtifact):
        return record.fixture_manifest
    artifact = EvalFixtureManifestArtifact.model_validate(
        _artifact_record_mapping(record)
    )
    return artifact.fixture_manifest


def _validate_present_closure_artifacts(
    artifact_bundle: Mapping[Any, Any],
) -> tuple[dict[str, BaseModel], tuple[str, ...]]:
    from millforge.eval_artifacts import validate_eval_artifact_record

    artifacts = _artifact_bundle_items(artifact_bundle)
    validated: dict[str, BaseModel] = {}
    diagnostics: list[str] = []
    for artifact_id, record in artifacts.items():
        try:
            validated[artifact_id] = validate_eval_artifact_record(
                artifact_id, _artifact_record_mapping(record)
            )
        except (TypeError, ValueError) as exc:
            diagnostics.append(f"{artifact_id}: {exc}")
    return validated, tuple(diagnostics)


def _required_closure_artifact_ids(
    outcome_kind: EvalClosureOutcomeKind,
) -> tuple[str, ...]:
    from millforge.eval_artifacts import EVAL_LOGICAL_06A_ARTIFACT_IDS

    base_artifact_ids = (
        "task",
        "fixture_manifest",
        "acceptance_checks",
        "plan",
        "arbiter_verdict",
        "context_snapshot",
    )
    if outcome_kind == EvalClosureOutcomeKind.VALID_BLOCKED_OUTCOME:
        return base_artifact_ids
    return EVAL_LOGICAL_06A_ARTIFACT_IDS + ("context_snapshot",)


def _capability_snapshot_results(
    capability_snapshot: Any,
) -> tuple[EvalCapabilityValidationResult, ...]:
    if isinstance(capability_snapshot, Mapping) and "results" in capability_snapshot:
        return _capability_snapshot_results(capability_snapshot["results"])
    if isinstance(capability_snapshot, Mapping):
        results: list[EvalCapabilityValidationResult] = []
        for raw_stage_id, capabilities in capability_snapshot.items():
            stage_id = _coerce_stage_id(raw_stage_id)
            if isinstance(capabilities, EvalCapabilityEnvelope):
                capability_ids = capabilities.capability_ids
            elif isinstance(capabilities, Mapping) and "capability_ids" in capabilities:
                capability_ids = tuple(capabilities["capability_ids"])
            else:
                capability_ids = tuple(capabilities)
            for capability in capability_ids:
                results.append(validate_eval_stage_capability(stage_id, capability))
        return tuple(results)
    results = []
    for item in tuple(capability_snapshot):
        if isinstance(item, EvalCapabilityValidationResult):
            results.append(item)
        elif isinstance(item, EvalCapabilityEnvelope):
            results.extend(
                validate_eval_stage_capability(item.stage_id, capability)
                for capability in item.capability_ids
            )
        elif isinstance(item, Mapping) and "allowed" in item:
            results.append(EvalCapabilityValidationResult.model_validate(item))
        elif isinstance(item, Mapping) and "capability_ids" in item:
            stage_id = item["stage_id"]
            results.extend(
                validate_eval_stage_capability(stage_id, capability)
                for capability in item["capability_ids"]
            )
        else:
            raise TypeError("capability snapshot entries are not recognized")
    return tuple(results)


def _capability_snapshot_diagnostics(capability_snapshot: Any) -> tuple[str, ...]:
    try:
        results = _capability_snapshot_results(capability_snapshot)
    except TypeError as exc:
        return (str(exc),)
    if not results:
        return ("capability snapshot must include admitted capability evidence",)
    result_stage_ids = {
        result.stage_id
        for result in results
        if isinstance(result.stage_id, EvalStageId)
    }
    missing_stage_ids = tuple(
        stage_id.value for stage_id in EvalStageId if stage_id not in result_stage_ids
    )
    diagnostics = [
        f"{result.stage_id}: {result.capability_id}: {result.rule_id}"
        for result in results
        if not result.allowed
    ]
    if missing_stage_ids:
        diagnostics.append(
            "capability snapshot missing stages: " + ", ".join(missing_stage_ids)
        )
    return tuple(diagnostics)


def _fixture_snapshot_diagnostics(
    fixture_snapshot: EvalFixtureWorkspaceSnapshot | Mapping[str, Any],
    artifact_bundle: Mapping[Any, Any],
) -> tuple[str, ...]:
    snapshot = (
        fixture_snapshot
        if isinstance(fixture_snapshot, EvalFixtureWorkspaceSnapshot)
        else EvalFixtureWorkspaceSnapshot.model_validate(fixture_snapshot)
    )
    diagnostics: list[str] = []
    if snapshot.unauthorized_mutation_paths:
        diagnostics.append("fixture snapshot includes unauthorized mutations")
    if snapshot.violations:
        diagnostics.append("fixture snapshot includes path policy violations")

    artifacts = _artifact_bundle_items(artifact_bundle)
    manifest_record = artifacts.get("fixture_manifest")
    if manifest_record is not None:
        manifest = _fixture_manifest_from_artifact_record(manifest_record)
        if snapshot.fixture_id != manifest.fixture_id:
            diagnostics.append("fixture snapshot fixture_id does not match manifest")
        if snapshot.fixture_manifest_sha256 != eval_fixture_manifest_sha256(manifest):
            diagnostics.append(
                "fixture snapshot manifest digest does not match expanded manifest"
            )
    return tuple(diagnostics)


def _fixture_snapshot_mutation_paths(
    fixture_snapshot: EvalFixtureWorkspaceSnapshot | Mapping[str, Any],
) -> tuple[str, ...]:
    snapshot = (
        fixture_snapshot
        if isinstance(fixture_snapshot, EvalFixtureWorkspaceSnapshot)
        else EvalFixtureWorkspaceSnapshot.model_validate(fixture_snapshot)
    )
    return snapshot.added_paths + snapshot.modified_paths + snapshot.deleted_paths


def _context_boundary_diagnostics(
    validated_artifacts: Mapping[str, BaseModel],
) -> tuple[str, ...]:
    acceptance = validated_artifacts.get("acceptance_checks")
    context = validated_artifacts.get("context_snapshot")
    if acceptance is None or context is None:
        return ("closure context validation requires acceptance and context artifacts",)

    visible_checks = tuple(
        check.check_id for check in getattr(acceptance, "visible_acceptance_checks", ())
    )
    context_visible_checks = tuple(getattr(context, "visible_acceptance_check_ids", ()))
    if not visible_checks:
        return ("visible acceptance checks must be declared",)
    if set(context_visible_checks) != set(visible_checks):
        return (
            "context visible acceptance check IDs must match acceptance artifact IDs",
        )
    return ()


def _closure_evidence_artifact_ids(
    validated_artifacts: Mapping[str, BaseModel],
) -> tuple[str, ...]:
    arbiter_verdict = validated_artifacts["arbiter_verdict"]
    references = getattr(arbiter_verdict, "closure_evidence_references", ())
    return tuple(reference.artifact_id.value for reference in references)


def _closure_outcome_kind(
    validated_artifacts: Mapping[str, BaseModel],
) -> tuple[EvalClosureOutcomeKind, EvalTerminalResult, EvalCandidateDisposition]:
    from millforge.eval_artifacts import EvalArbiterVerdictValue

    arbiter_verdict = validated_artifacts["arbiter_verdict"]
    verdict = arbiter_verdict.verdict
    disposition = arbiter_verdict.candidate_disposition
    if verdict == EvalArbiterVerdictValue.BLOCKED:
        return (
            EvalClosureOutcomeKind.VALID_BLOCKED_OUTCOME,
            EvalTerminalResult.ARBITER_BLOCKED,
            EvalCandidateDisposition.BLOCKED,
        )
    if verdict == EvalArbiterVerdictValue.REJECTED:
        return (
            EvalClosureOutcomeKind.VALID_CLOSED_REJECTION,
            EvalTerminalResult.ARBITER_REJECTED,
            EvalCandidateDisposition.REJECTED,
        )
    if disposition == EvalCandidateDisposition.REJECTED:
        return (
            EvalClosureOutcomeKind.VALID_CLOSED_REJECTION,
            EvalTerminalResult.ARBITER_REJECTED,
            disposition,
        )
    return (
        EvalClosureOutcomeKind.VALID_CLOSED_SUCCESS,
        EvalTerminalResult.ARBITER_CLOSED,
        disposition,
    )


def _terminal_path_diagnostics(
    validated_artifacts: Mapping[str, BaseModel],
    outcome_kind: EvalClosureOutcomeKind,
) -> tuple[str, ...]:
    from millforge.eval_artifacts import EvalArbiterVerdictValue

    arbiter_verdict = validated_artifacts.get("arbiter_verdict")
    if arbiter_verdict is None:
        return ("arbiter verdict is required to prove terminal path",)

    verdict = arbiter_verdict.verdict
    disposition = arbiter_verdict.candidate_disposition
    if verdict == EvalArbiterVerdictValue.BLOCKED and (
        outcome_kind != EvalClosureOutcomeKind.VALID_BLOCKED_OUTCOME
        or disposition != EvalCandidateDisposition.BLOCKED
    ):
        return ("blocked terminal path requires blocked candidate disposition",)
    if verdict != EvalArbiterVerdictValue.BLOCKED and (
        outcome_kind == EvalClosureOutcomeKind.VALID_BLOCKED_OUTCOME
        or disposition == EvalCandidateDisposition.BLOCKED
    ):
        return ("blocked candidate disposition requires blocked arbiter verdict",)
    return ()


def validate_eval_closure(
    artifact_bundle: Mapping[Any, Any],
    fixture_snapshot: EvalFixtureWorkspaceSnapshot | Mapping[str, Any],
    capability_snapshot: Any,
) -> EvalClosureValidationResult:
    """Validate compact eval closure evidence without mutating workspace state."""
    validated_artifacts, artifact_diagnostics = _validate_present_closure_artifacts(
        artifact_bundle
    )
    if artifact_diagnostics:
        return _closure_result(
            EvalClosureOutcomeKind.INVALID_ARTIFACT_BOUNDARY,
            diagnostics=artifact_diagnostics,
        )

    base_required_artifact_ids = _required_closure_artifact_ids(
        EvalClosureOutcomeKind.VALID_BLOCKED_OUTCOME
    )
    base_missing_artifact_ids = tuple(
        artifact_id
        for artifact_id in base_required_artifact_ids
        if artifact_id not in validated_artifacts
    )
    if base_missing_artifact_ids:
        return _closure_result(
            EvalClosureOutcomeKind.INVALID_ARTIFACT_BOUNDARY,
            missing_artifact_ids=base_missing_artifact_ids,
            diagnostics=(
                "closure artifact bundle is missing required terminal-path artifacts",
            ),
        )

    outcome_kind, terminal_result, candidate_disposition = _closure_outcome_kind(
        validated_artifacts
    )
    terminal_path_diagnostics = _terminal_path_diagnostics(
        validated_artifacts, outcome_kind
    )
    if terminal_path_diagnostics:
        return _closure_result(
            EvalClosureOutcomeKind.INVALID_ARTIFACT_BOUNDARY,
            diagnostics=terminal_path_diagnostics,
        )

    required_artifact_ids = _required_closure_artifact_ids(outcome_kind)
    missing_artifact_ids = tuple(
        artifact_id
        for artifact_id in required_artifact_ids
        if artifact_id not in validated_artifacts
    )
    if missing_artifact_ids:
        return _closure_result(
            EvalClosureOutcomeKind.INVALID_ARTIFACT_BOUNDARY,
            missing_artifact_ids=missing_artifact_ids,
            diagnostics=(
                "closure artifact bundle is missing required terminal-path artifacts",
            ),
        )

    relaxed_blocked_artifact_ids = (
        "workspace_diff",
        "patch_summary",
        "test_results",
        "checker_verdict",
    )
    using_relaxed_blocked_artifacts = (
        outcome_kind == EvalClosureOutcomeKind.VALID_BLOCKED_OUTCOME
        and any(
            artifact_id not in validated_artifacts
            for artifact_id in relaxed_blocked_artifact_ids
        )
    )
    if using_relaxed_blocked_artifacts:
        try:
            mutation_paths = _fixture_snapshot_mutation_paths(fixture_snapshot)
        except (TypeError, ValueError) as exc:
            return _closure_result(
                EvalClosureOutcomeKind.INVALID_FIXTURE_BOUNDARY,
                diagnostics=(str(exc),),
            )
        if mutation_paths:
            return _closure_result(
                EvalClosureOutcomeKind.INVALID_ARTIFACT_BOUNDARY,
                diagnostics=(
                    "shortened blocked terminal path requires unmodified fixture snapshot",
                ),
            )

    capability_diagnostics = _capability_snapshot_diagnostics(capability_snapshot)
    if capability_diagnostics:
        return _closure_result(
            EvalClosureOutcomeKind.INVALID_CAPABILITY_BOUNDARY,
            diagnostics=capability_diagnostics,
        )

    try:
        fixture_diagnostics = _fixture_snapshot_diagnostics(
            fixture_snapshot, artifact_bundle
        )
    except (TypeError, ValueError) as exc:
        fixture_diagnostics = (str(exc),)
    if fixture_diagnostics:
        return _closure_result(
            EvalClosureOutcomeKind.INVALID_FIXTURE_BOUNDARY,
            diagnostics=fixture_diagnostics,
        )

    context_diagnostics = _context_boundary_diagnostics(validated_artifacts)
    if context_diagnostics:
        return _closure_result(
            EvalClosureOutcomeKind.INVALID_CONTEXT_BOUNDARY,
            diagnostics=context_diagnostics,
        )

    evidence_artifact_ids = _closure_evidence_artifact_ids(validated_artifacts)
    if not evidence_artifact_ids or any(
        artifact_id not in validated_artifacts for artifact_id in evidence_artifact_ids
    ):
        return _closure_result(
            EvalClosureOutcomeKind.INVALID_ARTIFACT_BOUNDARY,
            diagnostics=(
                "arbiter closure evidence references must resolve inside artifact bundle",
            ),
        )

    return _closure_result(
        outcome_kind,
        terminal_result=terminal_result,
        candidate_disposition=candidate_disposition,
        evidence_artifact_ids=evidence_artifact_ids,
    )


__all__ = [
    "AUTHORITATIVE_COMPACT_EVAL_WORKFLOW_NAMES",
    "EVAL_BOUNDARY_ARTIFACT_MODULE_DECISION",
    "EVAL_BOUNDARY_ARTIFACT_MODULE_REQUIRED",
    "EVAL_BOUNDARY_MODULE_NAME",
    "EVAL_BUILDER_DEFAULT_WRITE_ROOTS",
    "EVAL_CHECKER_IGNORED_SCRATCH_ROOTS",
    "EVAL_CONTEXT_DEFAULT_ALLOWED_PATHS",
    "EVAL_CONTEXT_DEFAULT_REDACTION_CATEGORIES",
    "EVAL_CONTEXT_DEFAULT_REQUIRED_ARTIFACT_IDS",
    "EVAL_CONTEXT_FINGERPRINT_KIND",
    "EVAL_DENIED_CAPABILITY_IDS",
    "EVAL_FIXTURE_IGNORED_GENERATED_ROOTS",
    "EVAL_FIXTURE_IGNORED_GENERATED_SUFFIXES",
    "EVAL_STAGE_RESOURCE_CEILING_DEFAULTS",
    "EVAL_TRIAL_RESOURCE_CEILING_DEFAULTS",
    "EVAL_WORKSPACE_ISOLATION_CONTRACT",
    "EvalBoundaryBaseline",
    "EvalBoundaryStageArtifacts",
    "EvalCapabilityEnvelope",
    "EvalCapabilityId",
    "EvalCapabilityValidationResult",
    "EvalCommandAdmissionResult",
    "EvalCommandDescriptor",
    "EvalCommandEnvironmentPolicy",
    "EvalContextArtifactSummary",
    "EvalContextRedaction",
    "EvalContextSnapshot",
    "EvalContextTier",
    "EvalClosureOutcomeKind",
    "EvalClosureValidationResult",
    "EvalFixtureFile",
    "EvalFixtureManifest",
    "EvalFixtureWorkspacePolicy",
    "EvalFixtureWorkspaceSnapshot",
    "EvalPathPolicyViolation",
    "EvalResourceCeiling",
    "EvalStageContextPolicy",
    "build_eval_context_snapshot",
    "calculate_eval_context_fingerprint",
    "compact_eval_boundary_baseline",
    "compact_eval_boundary_baseline_snapshot",
    "default_eval_capability_envelopes",
    "default_eval_stage_context_policies",
    "default_eval_stage_context_policy",
    "default_eval_stage_resource_ceiling",
    "default_eval_trial_resource_ceiling",
    "eval_fixture_file_hash",
    "eval_fixture_file_record",
    "eval_fixture_manifest_from_paths",
    "eval_fixture_manifest_sha256",
    "eval_fixture_workspace_snapshot",
    "eval_stage_capability_envelope",
    "validate_eval_fixture_path",
    "validate_eval_closure",
    "validate_eval_stage_capability",
    "validate_eval_stage_command",
]
