"""Composition boundary for the unrestricted Millforge base preset."""

from __future__ import annotations

import datetime
import hashlib
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
)

from millforge.compiled_plan import CompiledHarnessPlan, CompiledModelProfile
from millforge.compiler.catalogs import ModelProfileCatalogLookup, ToolCatalogSnapshot
from millforge.compiler.service import compile_harness_source_in_memory
from millforge.compiler.source import HarnessSource
from millforge.contracts import CapabilityEnvelope, CapabilityGrant
from millforge.model_backend import (
    CapabilitySupport,
    ResolvedModelProfile,
    UnsupportedModelCapabilityError,
)
from millforge.protocols import CancellationResolver
from millforge.tools.execution import CompiledToolBindingExecutor
from millforge.tools.pi_compat.process import (
    PiCompatShellConfig,
    resolve_pi_compat_shell,
)
from millforge.tools.pi_compat_catalog import (
    PI_COMPAT_TOOL_DESCRIPTORS,
    create_pi_compat_tool_snapshot,
)
from millforge.tools.pi_compat_runtime import create_pi_compat_tool_executor

from .context import (
    MillforgeBaseContextSnapshot,
    _load_millforge_base_context_resolved,
)
from .harness import (
    _CONFIG_ID,
    _HARNESS_ID,
    _STAGE_KIND,
    _TOOL_PACK_ID,
    millforge_base_harness_source,
)
from .options import MillforgeBaseOptions
from .platform import _require_supported_platform
from .prompt import (
    MillforgeBasePromptSnapshot,
    _build_millforge_base_system_prompt_resolved,
)

__all__ = [
    "MillforgeBaseComponents",
    "MillforgeBaseMetadata",
    "create_millforge_base_components",
]

_UPSTREAM_PACKAGE = "@earendil-works/pi-coding-agent"
_UPSTREAM_VERSION = "0.79.6"
_COMPATIBILITY_CLAIM = (
    "A Python behavioral port of Pi 0.79.6's complete built-in coding tool pack, "
    "adapted to Millforge's compiler and runtime contracts."
)
_SECURITY_WARNING = (
    "millforge-base runs with the permissions of the Millforge process. It can read, "
    "write, delete, execute commands, access the network, and access credentials "
    "available to that process. Use only in trusted environments."
)
_ENABLED_ALIASES = (
    "read",
    "bash",
    "edit",
    "write",
    "grep",
    "find",
    "ls",
    "submit",
    "block",
    "reject",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class MillforgeBaseMetadata(BaseModel):
    """Sanitized, deterministic metadata for one composed base preset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    config_id: StrictStr
    harness_id: StrictStr
    tool_pack_id: StrictStr
    upstream_package: StrictStr
    upstream_version: StrictStr
    compatibility_claim: StrictStr
    security_warning: StrictStr
    unrestricted: Literal[True]
    enabled_aliases: tuple[StrictStr, ...]
    model_profile_id: StrictStr
    provider_id: StrictStr
    model_id: StrictStr
    transport_id: StrictStr
    os_name: StrictStr
    shell_name: StrictStr
    cwd_sha256: StrictStr
    descriptor_snapshot_sha256: StrictStr
    compiled_sha256: StrictStr
    effective_prompt_sha256: StrictStr
    context_sha256: StrictStr
    context_file_count: StrictInt = Field(ge=0)
    context_truncated: StrictBool
    prompt_truncated: StrictBool

    @field_validator(
        "config_id",
        "harness_id",
        "tool_pack_id",
        "upstream_package",
        "upstream_version",
        "compatibility_claim",
        "security_warning",
        "model_profile_id",
        "provider_id",
        "model_id",
        "transport_id",
        "os_name",
        "shell_name",
    )
    @classmethod
    def _strings_are_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("metadata strings must be nonblank")
        return value

    @field_validator("enabled_aliases")
    @classmethod
    def _aliases_are_nonblank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not alias.strip() for alias in value):
            raise ValueError("enabled_aliases must contain only nonblank values")
        return value

    @field_validator(
        "cwd_sha256",
        "descriptor_snapshot_sha256",
        "compiled_sha256",
        "effective_prompt_sha256",
        "context_sha256",
    )
    @classmethod
    def _hashes_are_lowercase_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("metadata hashes must be lowercase SHA-256 values")
        return value


@dataclass(frozen=True)
class MillforgeBaseComponents:
    """Fully composed transient components for one base-preset invocation."""

    options: MillforgeBaseOptions
    context: MillforgeBaseContextSnapshot
    prompt: MillforgeBasePromptSnapshot
    harness_source: HarnessSource
    compiled_plan: CompiledHarnessPlan
    model_profile: ResolvedModelProfile
    tool_snapshot: ToolCatalogSnapshot
    tool_executor: CompiledToolBindingExecutor
    capability_envelope: CapabilityEnvelope
    metadata: MillforgeBaseMetadata


@dataclass(frozen=True)
class _SingleModelProfileCatalog:
    """One admitted logical model profile for a single in-memory compilation."""

    profile: CompiledModelProfile
    snapshot_id: str
    snapshot_sha256: str

    def resolve_exact(self, profile_id: str) -> ModelProfileCatalogLookup:
        if profile_id == self.profile.profile_id:
            return ModelProfileCatalogLookup.found(self.profile)
        return ModelProfileCatalogLookup.missing(
            error_code="profile.missing",
            evidence={"profile_id": profile_id},
        )


def _resolved_absolute_path(path: Path, field_name: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{field_name} must be absolute")
    return path.resolve()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _capability_envelope() -> CapabilityEnvelope:
    required_capabilities = sorted(
        {
            capability_id
            for descriptor in PI_COMPAT_TOOL_DESCRIPTORS
            for capability_id in descriptor.required_capabilities
        }
    )
    return CapabilityEnvelope(
        grants=tuple(
            CapabilityGrant(capability_id=capability_id)
            for capability_id in required_capabilities
        )
    )


def _model_profile_catalog(profile_id: str) -> _SingleModelProfileCatalog:
    snapshot_id = _sha256(f"millforge-base.model-profile:{profile_id}")
    return _SingleModelProfileCatalog(
        profile=CompiledModelProfile(profile_id=profile_id),
        snapshot_id=snapshot_id,
        snapshot_sha256=_sha256(f"millforge-base.model-profile.snapshot:{profile_id}"),
    )


def _metadata(
    *,
    model_profile: ResolvedModelProfile,
    cwd: Path,
    shell_config: PiCompatShellConfig,
    context: MillforgeBaseContextSnapshot,
    prompt: MillforgeBasePromptSnapshot,
    tool_snapshot: ToolCatalogSnapshot,
    compiled_plan: CompiledHarnessPlan,
) -> MillforgeBaseMetadata:
    return MillforgeBaseMetadata(
        schema_version=1,
        config_id=_CONFIG_ID,
        harness_id=_HARNESS_ID,
        tool_pack_id=_TOOL_PACK_ID,
        upstream_package=_UPSTREAM_PACKAGE,
        upstream_version=_UPSTREAM_VERSION,
        compatibility_claim=_COMPATIBILITY_CLAIM,
        security_warning=_SECURITY_WARNING,
        unrestricted=True,
        enabled_aliases=_ENABLED_ALIASES,
        model_profile_id=model_profile.profile_id,
        provider_id=model_profile.provider_id,
        model_id=model_profile.model_id,
        transport_id=model_profile.transport_id,
        os_name=platform.system().lower(),
        shell_name=Path(shell_config.executable).name,
        cwd_sha256=_sha256(cwd.as_posix()),
        descriptor_snapshot_sha256=tool_snapshot.snapshot_sha256,
        compiled_sha256=compiled_plan.compiled_sha256,
        effective_prompt_sha256=prompt.effective_prompt_sha256,
        context_sha256=context.context_sha256,
        context_file_count=len(context.files),
        context_truncated=context.truncated,
        prompt_truncated=prompt.truncated,
    )


def create_millforge_base_components(
    *,
    model_profile: ResolvedModelProfile,
    cwd: Path,
    cancellation_resolver: CancellationResolver,
    options: MillforgeBaseOptions | None = None,
    prompt_date: datetime.date | None = None,
    home_directory: Path | None = None,
) -> MillforgeBaseComponents:
    """Compose the unrestricted preset without making a provider request."""

    _require_supported_platform()

    unsupported_model_capabilities = tuple(
        capability
        for capability in ("tool_calls", "system_messages", "tool_result_messages")
        if model_profile.capabilities.state_for(capability)
        is not CapabilitySupport.SUPPORTED
    )
    if unsupported_model_capabilities:
        raise UnsupportedModelCapabilityError(
            "millforge-base requires supported model capabilities: "
            + ", ".join(unsupported_model_capabilities)
        )

    resolved_cwd = _resolved_absolute_path(cwd, "cwd")
    resolved_home = _resolved_absolute_path(
        Path.home() if home_directory is None else home_directory,
        "home_directory",
    )
    effective_options = options or MillforgeBaseOptions()
    effective_prompt_date = prompt_date or datetime.date.today()
    context = _load_millforge_base_context_resolved(
        cwd=resolved_cwd,
        home_directory=resolved_home,
        enabled=effective_options.load_context_files,
    )
    prompt = _build_millforge_base_system_prompt_resolved(
        options=effective_options,
        context=context,
        cwd=resolved_cwd,
        home_directory=resolved_home,
        prompt_date=effective_prompt_date,
    )
    shell_config = resolve_pi_compat_shell()
    source = millforge_base_harness_source(
        model_profile_id=model_profile.profile_id,
        system_instructions=prompt.system_instructions,
    )
    tool_snapshot = create_pi_compat_tool_snapshot()
    capability_envelope = _capability_envelope()
    compiled_plan = compile_harness_source_in_memory(
        request_id="millforge-base.v1",
        source=source,
        stage_kind_id=_STAGE_KIND,
        legal_terminal_results=("COMPLETE", "BLOCKED", "REJECTED"),
        capability_envelope=capability_envelope,
        tool_catalog=tool_snapshot,
        model_profile_catalog=_model_profile_catalog(model_profile.profile_id),
    )
    if tuple(
        grant.capability_id for grant in capability_envelope.grants
    ) != compiled_plan.required_capabilities or any(
        grant.constraints is not None for grant in capability_envelope.grants
    ):
        raise RuntimeError("millforge-base capability envelope does not match its plan")

    tool_executor = create_pi_compat_tool_executor(
        compiled_plan,
        cwd=resolved_cwd,
        cancellation_resolver=cancellation_resolver,
        shell_config=shell_config,
    )
    metadata = _metadata(
        model_profile=model_profile,
        cwd=resolved_cwd,
        shell_config=shell_config,
        context=context,
        prompt=prompt,
        tool_snapshot=tool_snapshot,
        compiled_plan=compiled_plan,
    )
    return MillforgeBaseComponents(
        options=effective_options,
        context=context,
        prompt=prompt,
        harness_source=source,
        compiled_plan=compiled_plan,
        model_profile=model_profile,
        tool_snapshot=tool_snapshot,
        tool_executor=tool_executor,
        capability_envelope=capability_envelope,
        metadata=metadata,
    )
