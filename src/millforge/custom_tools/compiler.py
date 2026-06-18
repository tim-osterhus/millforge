"""Deterministic offline compilation for custom-tool source manifests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from millforge.compiler.diagnostics import detect_secret_candidate
from millforge.compiler.schema_validation import (
    SchemaSubsetError,
    normalized_schema_bytes,
)
from millforge.contracts import RedactionPolicy
from millforge.custom_tools.contracts import (
    CustomToolApprovalPolicy,
    CustomToolCompilationRecord,
    CustomToolCompilationResult,
    CustomToolCompilerPolicy,
    CustomToolDeclaration,
    CustomToolSourceManifest,
    compilation_record_from_declaration,
    tool_descriptor_from_declaration,
)
from millforge.custom_tools.diagnostics import (
    CustomToolDiagnostic,
    CustomToolDiagnosticCode,
    CustomToolDiagnosticPhase,
    custom_tool_diagnostic,
    custom_tool_diagnostic_sort_key,
    malformed_input_diagnostic,
)
from millforge.tools.registry import ToolDescriptor

_T = TypeVar("_T", bound=BaseModel)

_LIVE_URL_RE = re.compile(r"\bhttps?://[^\s<>'\"]+", re.IGNORECASE)
_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])(?:/[A-Za-z0-9_.-][^\s]*)")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s]+")
_PARENT_TRAVERSAL_RE = re.compile(r"(^|[\s\\/])\.\.([\\/]|$)")
_SHELL_COMMAND_RE = re.compile(
    r"(?i)(^|\s)(?:rm\s+-rf|curl\s+|wget\s+|bash\s+-c|sh\s+-c|"
    r"python(?:3)?\s+-c|node\s+-e|powershell\b|cmd\.exe\b|chmod\s+\+x)\b"
)
_SCRIPT_BODY_RE = re.compile(
    r"(?is)(^#!|<script\b|function\s+\w*\s*\(|def\s+\w+\s*\(|"
    r"import\s+os\b|subprocess\.|eval\s*\(|exec\s*\()"
)
_TEMPLATE_INTERPOLATION_RE = re.compile(r"({{.*?}}|{%.+?%}|\$\{[^}]+})")
_INSTRUCTION_LIKE_RE = re.compile(
    r"\b(ignore|override|system prompt|developer message|previous instructions|"
    r"follow these instructions|you must|do not tell)\b",
    re.IGNORECASE,
)
_EXECUTABLE_RUNTIME_KINDS = frozenset(
    {
        "shell",
        "process",
        "python",
        "javascript",
        "js",
        "node",
        "wasm",
        "http",
        "https",
        "mcp",
        "connector",
        "connector_alias",
        "filesystem",
        "fs",
        "terminal",
    }
)


def compile_custom_tools(
    source: CustomToolSourceManifest | Mapping[str, Any],
    policy: CustomToolCompilerPolicy | Mapping[str, Any],
) -> CustomToolCompilationResult:
    """Validate and lower contract-only custom tools into descriptors.

    Raw mappings are treated as untrusted source. Validation and lowering errors
    are returned as stable custom-tool diagnostics, and any diagnostic rejects
    the whole manifest with no partial descriptors or records.
    """
    source_hazards = _raw_source_hazards(source, phase=CustomToolDiagnosticPhase.SOURCE)
    policy_hazards = _raw_source_hazards(policy, phase=CustomToolDiagnosticPhase.POLICY)
    if source_hazards or policy_hazards:
        return _rejected((*source_hazards, *policy_hazards))

    valid_source, source_diagnostic = _validate_contract(
        CustomToolSourceManifest,
        source,
        phase=CustomToolDiagnosticPhase.SOURCE,
    )
    valid_policy, policy_diagnostic = _validate_contract(
        CustomToolCompilerPolicy,
        policy,
        phase=CustomToolDiagnosticPhase.POLICY,
    )
    diagnostics = tuple(
        diagnostic
        for diagnostic in (source_diagnostic, policy_diagnostic)
        if diagnostic
    )
    if diagnostics:
        return _rejected(diagnostics)
    if not isinstance(valid_source, CustomToolSourceManifest):
        raise AssertionError("source validation returned unexpected contract")
    if not isinstance(valid_policy, CustomToolCompilerPolicy):
        raise AssertionError("policy validation returned unexpected contract")

    compiler = _CustomToolCompilation(valid_source, valid_policy)
    return compiler.run()


class _CustomToolCompilation:
    def __init__(
        self,
        source: CustomToolSourceManifest,
        policy: CustomToolCompilerPolicy,
    ) -> None:
        self.source = source
        self.policy = policy
        self.diagnostics: list[CustomToolDiagnostic] = []

    def run(self) -> CustomToolCompilationResult:
        self._validate_source_policy()

        lowered: list[tuple[ToolDescriptor, CustomToolCompilationRecord]] = []
        for index, declaration in enumerate(self.source.tools):
            compiled = self._lower_declaration(declaration, index=index)
            if compiled is not None:
                lowered.append(compiled)

        if self.diagnostics:
            return self._rejected()

        return CustomToolCompilationResult(
            accepted=True,
            source_sha256=self.source.source_sha256,
            descriptors=tuple(descriptor for descriptor, _ in _sort_lowered(lowered)),
            records=tuple(record for _, record in _sort_lowered(lowered)),
        )

    def _validate_source_policy(self) -> None:
        if len(self.source.tools) > self.policy.max_tools:
            self._diagnose(
                CustomToolDiagnosticCode.LIMIT_EXCEEDED,
                phase=CustomToolDiagnosticPhase.POLICY,
                path="/tools",
                message="Custom-tool source exceeds the compiler policy tool limit.",
                evidence={"limit": self.policy.max_tools},
            )
        self._check_hash(
            supplied=self.source.expected_source_sha256,
            actual=self.source.source_sha256,
            path="/expected_source_sha256",
            label="source_sha256",
        )
        if (
            self.policy.require_expected_hashes
            and self.source.expected_source_sha256 is None
        ):
            self._missing_hash("/expected_source_sha256", "source_sha256")

        produced_artifacts: dict[str, str] = {}
        for declaration in self.source.tools:
            for artifact_id in declaration.produced_artifact_ids:
                prior = produced_artifacts.get(artifact_id)
                if prior is not None:
                    self._diagnose(
                        CustomToolDiagnosticCode.ARTIFACT_POLICY_INVALID,
                        phase=CustomToolDiagnosticPhase.SOURCE,
                        path="/tools",
                        message="Custom-tool source contains duplicate produced artifacts.",
                        evidence={"artifact_id": artifact_id, "first_tool_id": prior},
                    )
                else:
                    produced_artifacts[artifact_id] = declaration.tool_id

    def _lower_declaration(
        self,
        declaration: CustomToolDeclaration,
        *,
        index: int,
    ) -> tuple[ToolDescriptor, CustomToolCompilationRecord] | None:
        path = f"/tools/{index}"
        if declaration.runtime_kind not in self.policy.allowed_runtime_kinds:
            self._diagnose(
                CustomToolDiagnosticCode.RUNTIME_KIND_UNSUPPORTED,
                phase=CustomToolDiagnosticPhase.POLICY,
                path=f"{path}/runtime_kind",
                message="Custom-tool runtime kind is not allowed by compiler policy.",
                evidence={"runtime_kind": declaration.runtime_kind.value},
            )
        if (
            len(declaration.description.encode("utf-8"))
            > self.policy.max_description_utf8
        ):
            self._diagnose(
                CustomToolDiagnosticCode.LIMIT_EXCEEDED,
                phase=CustomToolDiagnosticPhase.POLICY,
                path=f"{path}/description",
                message="Custom-tool description exceeds the compiler policy limit.",
                evidence={"tool_id": declaration.tool_id},
            )
        self._validate_schema_bytes(
            declaration.input_schema,
            path=f"{path}/input_schema",
            code=CustomToolDiagnosticCode.INPUT_SCHEMA_UNSUPPORTED,
        )
        self._validate_schema_bytes(
            declaration.output_schema,
            path=f"{path}/output_schema",
            code=CustomToolDiagnosticCode.OUTPUT_SCHEMA_UNSUPPORTED,
        )
        self._validate_capabilities(declaration, path=path)
        self._validate_approval(declaration, path=path)
        self._check_hash(
            supplied=declaration.expected_declaration_sha256,
            actual=declaration.declaration_sha256,
            path=f"{path}/expected_declaration_sha256",
            label="declaration_sha256",
        )
        if (
            self.policy.require_expected_hashes
            and declaration.expected_declaration_sha256 is None
        ):
            self._missing_hash(
                f"{path}/expected_declaration_sha256", "declaration_sha256"
            )
        try:
            descriptor = tool_descriptor_from_declaration(declaration)
            self._check_hash(
                supplied=declaration.expected_descriptor_sha256,
                actual=descriptor.descriptor_sha256,
                path=f"{path}/expected_descriptor_sha256",
                label="descriptor_sha256",
            )
            if (
                self.policy.require_expected_hashes
                and declaration.expected_descriptor_sha256 is None
            ):
                self._missing_hash(
                    f"{path}/expected_descriptor_sha256", "descriptor_sha256"
                )
            record = compilation_record_from_declaration(
                self.source, declaration, descriptor
            )
        except Exception as exc:
            self._diagnose(
                CustomToolDiagnosticCode.DECLARATION_INVALID,
                phase=CustomToolDiagnosticPhase.COMPILATION,
                path=path,
                message="Custom-tool descriptor construction failed.",
                evidence={"error_type": type(exc).__name__},
            )
            return None

        self._check_hash(
            supplied=declaration.expected_compilation_record_sha256,
            actual=record.compilation_record_sha256,
            path=f"{path}/expected_compilation_record_sha256",
            label="compilation_record_sha256",
        )
        if (
            self.policy.require_expected_hashes
            and declaration.expected_compilation_record_sha256 is None
        ):
            self._missing_hash(
                f"{path}/expected_compilation_record_sha256",
                "compilation_record_sha256",
            )
        if self.diagnostics:
            return None
        return descriptor, record

    def _validate_schema_bytes(
        self,
        schema: Mapping[str, Any],
        *,
        path: str,
        code: CustomToolDiagnosticCode,
    ) -> None:
        size = len(normalized_schema_bytes(schema))
        if size <= self.policy.max_schema_bytes:
            return
        self._diagnose(
            code,
            phase=CustomToolDiagnosticPhase.POLICY,
            path=path,
            message="Custom-tool schema exceeds the compiler policy byte limit.",
            evidence={"limit": self.policy.max_schema_bytes, "size": size},
        )

    def _validate_capabilities(
        self, declaration: CustomToolDeclaration, *, path: str
    ) -> None:
        if (
            declaration.side_effect_class.value != "read_only"
            or declaration.produced_artifact_ids
        ) and not declaration.required_capabilities:
            self._diagnose(
                CustomToolDiagnosticCode.CAPABILITY_MISSING,
                phase=CustomToolDiagnosticPhase.POLICY,
                path=f"{path}/required_capabilities",
                message="Custom tool requires explicit capabilities.",
                evidence={"tool_id": declaration.tool_id},
            )
            return
        allowed = set(self.policy.allowed_capability_ids)
        for capability_id in declaration.required_capabilities:
            if capability_id not in allowed:
                self._diagnose(
                    CustomToolDiagnosticCode.CAPABILITY_UNKNOWN,
                    phase=CustomToolDiagnosticPhase.POLICY,
                    path=f"{path}/required_capabilities",
                    message="Custom-tool capability is not allowed by compiler policy.",
                    evidence={"capability_id": capability_id},
                )

    def _validate_approval(
        self, declaration: CustomToolDeclaration, *, path: str
    ) -> None:
        allowed = self.policy.side_effect_approval_matrix.get(
            declaration.side_effect_class
        )
        if allowed is None or declaration.approval_policy not in allowed:
            self._diagnose(
                CustomToolDiagnosticCode.APPROVAL_POLICY_INVALID,
                phase=CustomToolDiagnosticPhase.POLICY,
                path=f"{path}/approval_policy",
                message="Approval policy is not allowed for side-effect class.",
                evidence={
                    "approval_policy": declaration.approval_policy.value,
                    "side_effect_class": declaration.side_effect_class.value,
                },
            )
        if (
            declaration.produced_artifact_ids
            and declaration.approval_policy is CustomToolApprovalPolicy.NONE
        ):
            self._diagnose(
                CustomToolDiagnosticCode.APPROVAL_POLICY_INVALID,
                phase=CustomToolDiagnosticPhase.POLICY,
                path=f"{path}/approval_policy",
                message="Artifact-producing custom tools require explicit approval.",
                evidence={
                    "approval_policy": declaration.approval_policy.value,
                    "tool_id": declaration.tool_id,
                },
            )

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
                CustomToolDiagnosticCode.HASH_MISMATCH,
                phase=CustomToolDiagnosticPhase.COMPILATION,
                path=path,
                message="Supplied custom-tool hash does not match recomputed hash.",
                evidence={"hash": label},
            )

    def _missing_hash(self, path: str, label: str) -> None:
        self._diagnose(
            CustomToolDiagnosticCode.HASH_MISMATCH,
            phase=CustomToolDiagnosticPhase.POLICY,
            path=path,
            message="Compiler policy requires expected custom-tool hashes.",
            evidence={"hash": label},
        )

    def _diagnose(
        self,
        code: CustomToolDiagnosticCode,
        *,
        phase: CustomToolDiagnosticPhase,
        message: str,
        location: str | None = None,
        path: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        self.diagnostics.append(
            custom_tool_diagnostic(
                code,
                phase=phase,
                message=message,
                location=location,
                path=path,
                evidence=evidence,
            )
        )

    def _rejected(self) -> CustomToolCompilationResult:
        return _rejected(tuple(self.diagnostics))


def _validate_contract(
    model: type[_T],
    value: _T | Mapping[str, Any],
    *,
    phase: CustomToolDiagnosticPhase,
) -> tuple[_T | None, CustomToolDiagnostic | None]:
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
        return (
            None,
            malformed_input_diagnostic(
                phase=phase,
                model_name=model.__name__,
                path=_validation_pointer(exc),
                missing_field=_missing_field(exc),
                code=_validation_code(exc),
            ),
        )
    except Exception:
        return (
            None,
            malformed_input_diagnostic(
                phase=phase,
                model_name=model.__name__,
                code=CustomToolDiagnosticCode.SOURCE_INVALID,
            ),
        )


def _raw_source_hazards(
    value: Any, *, phase: CustomToolDiagnosticPhase
) -> tuple[CustomToolDiagnostic, ...]:
    if isinstance(value, BaseModel):
        try:
            value = value.model_dump(mode="json")
        except Exception:
            return (_hazard_diagnostic("/", phase, "runtime_object"),)
    diagnostics: list[CustomToolDiagnostic] = []
    _scan_raw_value(
        value,
        path="",
        phase=phase,
        diagnostics=diagnostics,
        active_container_ids=set(),
    )
    return tuple(diagnostics)


def _scan_raw_value(
    value: Any,
    *,
    path: str,
    phase: CustomToolDiagnosticPhase,
    diagnostics: list[CustomToolDiagnostic],
    active_container_ids: set[int],
) -> None:
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in active_container_ids:
            diagnostics.append(
                _hazard_diagnostic(path or "/", phase, "recursive_reference")
            )
            return
        active_container_ids.add(container_id)
        try:
            for key, item in value.items():
                key_path = _join_pointer(path, str(key))
                if not isinstance(key, str):
                    diagnostics.append(_hazard_diagnostic(key_path, phase, "non_json"))
                    continue
                _scan_raw_value(
                    item,
                    path=key_path,
                    phase=phase,
                    diagnostics=diagnostics,
                    active_container_ids=active_container_ids,
                )
        finally:
            active_container_ids.remove(container_id)
        return
    if isinstance(value, list | tuple):
        container_id = id(value)
        if container_id in active_container_ids:
            diagnostics.append(
                _hazard_diagnostic(path or "/", phase, "recursive_reference")
            )
            return
        active_container_ids.add(container_id)
        try:
            for index, item in enumerate(value):
                _scan_raw_value(
                    item,
                    path=_join_pointer(path, str(index)),
                    phase=phase,
                    diagnostics=diagnostics,
                    active_container_ids=active_container_ids,
                )
        finally:
            active_container_ids.remove(container_id)
        return
    if isinstance(value, str):
        hazard = _hazard_kind(value, field_name=path.rsplit("/", 1)[-1])
        if hazard is not None:
            diagnostics.append(_hazard_diagnostic(path or "/", phase, hazard))
        return
    if value is None or isinstance(value, bool | int | float):
        return
    diagnostics.append(_hazard_diagnostic(path or "/", phase, "runtime_object"))


def _hazard_kind(value: str, *, field_name: str) -> str | None:
    stripped = value.strip()
    if detect_secret_candidate(
        field_path=f"/{field_name}",
        field_name=field_name,
        value=stripped,
        policy=RedactionPolicy(),
    ):
        return "secret_material"
    if field_name == "runtime_kind" and stripped != "contract_only":
        if stripped.lower() in _EXECUTABLE_RUNTIME_KINDS:
            return "runtime_kind"
    if _LIVE_URL_RE.search(stripped):
        return "live_endpoint_url"
    if _PARENT_TRAVERSAL_RE.search(stripped):
        return "parent_traversal"
    if _SHELL_COMMAND_RE.search(stripped):
        return "shell_command"
    if _WINDOWS_ABSOLUTE_PATH_RE.search(stripped) or _ABSOLUTE_PATH_RE.search(stripped):
        return "absolute_path"
    if _SCRIPT_BODY_RE.search(stripped):
        return "script_body"
    if _TEMPLATE_INTERPOLATION_RE.search(stripped):
        return "template_interpolation"
    if field_name == "description" and _INSTRUCTION_LIKE_RE.search(stripped):
        return "instruction_like"
    return None


def _hazard_diagnostic(
    path: str, phase: CustomToolDiagnosticPhase, hazard: str
) -> CustomToolDiagnostic:
    code = _hazard_code(hazard)
    return custom_tool_diagnostic(
        code,
        phase=phase,
        path=path or "/",
        message="Custom tool source contains unsupported or hazardous material.",
        evidence={"hazard": hazard},
    )


def _hazard_code(hazard: str) -> CustomToolDiagnosticCode:
    if hazard == "secret_material":
        return CustomToolDiagnosticCode.SECRET_MATERIAL
    if hazard == "runtime_kind":
        return CustomToolDiagnosticCode.RUNTIME_KIND_UNSUPPORTED
    if hazard in {
        "absolute_path",
        "live_endpoint_url",
        "parent_traversal",
        "script_body",
        "shell_command",
        "template_interpolation",
    }:
        return CustomToolDiagnosticCode.EXECUTABLE_MATERIAL
    if hazard == "instruction_like":
        return CustomToolDiagnosticCode.DESCRIPTION_UNSAFE
    return CustomToolDiagnosticCode.SOURCE_MALFORMED


def _schema_subset_validation_diagnostic(
    exc: ValidationError,
    *,
    model_name: str,
    phase: CustomToolDiagnosticPhase,
) -> CustomToolDiagnostic | None:
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
            CustomToolDiagnosticCode.OUTPUT_SCHEMA_UNSUPPORTED
            if _validation_loc_has_field(loc, "output_schema")
            else CustomToolDiagnosticCode.INPUT_SCHEMA_UNSUPPORTED
        )
        return custom_tool_diagnostic(
            code,
            phase=phase,
            path=path,
            message="Custom-tool schema is outside the accepted JSON Schema subset.",
            evidence={
                "model": model_name,
                "error_type": type(schema_error).__name__,
                "schema_error": str(schema_error),
            },
        )
    return None


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


def _missing_field(exc: ValidationError) -> str | None:
    errors = exc.errors()
    if not errors or errors[0].get("type") != "missing":
        return None
    loc = errors[0].get("loc")
    if not isinstance(loc, tuple | list) or not loc:
        return None
    return str(loc[-1])


def _validation_pointer(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "/"
    return _validation_pointer_from_loc(errors[0].get("loc"))


def _validation_pointer_from_loc(loc: Any) -> str:
    if not isinstance(loc, tuple | list) or not loc:
        return "/"
    parts = [str(part).replace("~", "~0").replace("/", "~1") for part in loc]
    return "/" + "/".join(parts)


def _validation_loc_has_field(loc: Any, field_name: str) -> bool:
    if not isinstance(loc, tuple | list):
        return False
    return any(str(part) == field_name for part in loc)


def _join_pointer(prefix: str, raw_part: str) -> str:
    part = raw_part.replace("~", "~0").replace("/", "~1")
    return f"{prefix}/{part}" if prefix else f"/{part}"


def _rejected(
    diagnostics: tuple[CustomToolDiagnostic, ...],
) -> CustomToolCompilationResult:
    return CustomToolCompilationResult(
        accepted=False,
        diagnostics=tuple(sorted(diagnostics, key=custom_tool_diagnostic_sort_key)),
    )


def _sort_lowered(
    lowered: list[tuple[ToolDescriptor, CustomToolCompilationRecord]],
) -> tuple[tuple[ToolDescriptor, CustomToolCompilationRecord], ...]:
    return tuple(
        sorted(
            lowered,
            key=lambda item: (
                item[1].package_id,
                item[0].tool_id,
                item[0].tool_version,
                item[0].model_tool_name,
                item[0].implementation_id,
                item[0].descriptor_sha256,
                item[1].compilation_record_sha256,
            ),
        )
    )
