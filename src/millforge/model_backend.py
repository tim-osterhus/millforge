"""Internal provider-neutral model backend contracts.

This module is intentionally not re-exported from ``millforge.__init__``.
It may carry endpoint, authentication, secret, and transport details that the
public ``ModelClient`` protocol must not expose.
"""

from __future__ import annotations

import inspect
import json
import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, TypeAlias, cast, runtime_checkable
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from millforge.contracts import (
    AssistantMessage,
    InvalidToolArguments,
    JsonObject,
    JsonValue,
    ModelCapabilityRequirements,
    ModelCompletionRequest,
    ModelCompletionResponse,
    ModelMessage,
    ModelToolCall,
    ParsedToolArguments,
    SanitizedMetadataValue,
    SamplingRequest,
    SecretRef,
    TokenUsage,
)
from millforge.exceptions import (
    MillforgeConfigError,
    ModelTransportError,
)

_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"
_MAX_SANITIZED_VALUE_LENGTH = 512
_MAX_SANITIZED_FIELDS = 24
_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password)=([^&\s]+)"),
    re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+"),
    re.compile(r"\b(sk|pk|org|sess)-[a-zA-Z0-9]{8,}\b"),
)
_URL_PATTERN = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>'\"]+", re.IGNORECASE)
_SENSITIVE_FIELD_MARKERS = (
    "authorization",
    "api-key",
    "apikey",
    "token",
    "secret",
    "password",
    "credential",
    "cookie",
    "set-cookie",
)
_FORBIDDEN_CUSTOM_AUTH_HEADERS = {
    "accept",
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "host",
    "content-type",
    "user-agent",
}
JsonHeaders: TypeAlias = dict[str, str]
_FINISH_REASON_MAP = {
    "stop": "stop",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "length": "length",
    "content_filter": "content_filter",
    "cancelled": "cancelled",
}
_SUCCESS_BODY_LIMIT_BYTES = 4 * 1024 * 1024
_ERROR_BODY_LIMIT_BYTES = 64 * 1024
_DIRECT_SAMPLING_BODY_FIELDS = {
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "stop",
}


def _nonblank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _unique(values: tuple[str, ...], field_name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} values must be unique")


def _bounded(value: str, *, length: int = _MAX_SANITIZED_VALUE_LENGTH) -> str:
    return value if len(value) <= length else f"{value[:length]}...[truncated]"


class ModelBackendConfigError(MillforgeConfigError):
    """Invalid internal model backend configuration."""


class UnsupportedModelCapabilityError(ModelBackendConfigError):
    """A required model capability is not supported by the resolved profile."""


class SecretResolutionError(ModelBackendConfigError):
    """A configured model secret cannot be safely resolved."""


class ProviderErrorCategory(str, Enum):
    """Stable provider-neutral provider error categories."""

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    MALFORMED_RESPONSE = "malformed_response"
    SERVER_ERROR = "server_error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


_RETRYABLE_CATEGORIES = {
    ProviderErrorCategory.RATE_LIMIT,
    ProviderErrorCategory.TIMEOUT,
    ProviderErrorCategory.CONNECTION,
    ProviderErrorCategory.SERVER_ERROR,
}


@dataclass(frozen=True, slots=True)
class ModelProviderError(ModelTransportError):
    """Sanitized provider error data.

    ``message`` and ``fields`` must already be redacted. Raw response bodies,
    headers, exception reprs, and secret values are not accepted here.
    """

    category: ProviderErrorCategory
    message: str
    retryable: bool | None = None
    provider_request_id: str | None = None
    fields: Mapping[str, SanitizedMetadataValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", _bounded(redact_text(self.message)))
        if self.provider_request_id is not None:
            object.__setattr__(
                self,
                "provider_request_id",
                _bounded(redact_text(self.provider_request_id), length=128),
            )
        if self.retryable is None:
            object.__setattr__(
                self, "retryable", self.category in _RETRYABLE_CATEGORIES
            )
        sanitized = sanitize_provider_error_fields(self.fields)
        object.__setattr__(self, "fields", MappingProxyType(sanitized))
        Exception.__init__(self, self.message)

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return (
            "ModelProviderError("
            f"category={self.category.value!r}, retryable={self.retryable!r}, "
            f"provider_request_id={self.provider_request_id!r}, fields={dict(self.fields)!r})"
        )


class AuthenticationScheme(str, Enum):
    """Supported internal authentication policies."""

    NONE = "none"
    BEARER = "bearer"
    HEADER = "header"


class CapabilitySupport(str, Enum):
    """Tri-state capability support declaration."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class ReasoningSupport(str, Enum):
    """Provider-neutral reasoning support declaration."""

    UNSUPPORTED = "unsupported"
    OPTIONAL = "optional"
    REQUIRED = "required"


class ReasoningMode(str, Enum):
    """Canonical provider-neutral reasoning intent."""

    DISABLED = "disabled"
    ENABLED = "enabled"
    REQUIRED = "required"


class ReasoningEffort(str, Enum):
    """Canonical provider-neutral reasoning effort levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


class EndpointConfig(BaseModel):
    """Immutable normalized endpoint configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str
    allow_insecure_local: bool = False
    success_content_types: tuple[str, ...] = ("application/json",)
    allow_missing_success_content_type: bool = False

    @field_validator("base_url")
    @classmethod
    def _base_url_nonblank(cls, value: str) -> str:
        return _nonblank(value, "base_url").rstrip("/")

    @field_validator("success_content_types")
    @classmethod
    def _content_types_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("success_content_types must not be empty")
        normalized = tuple(item.strip().lower() for item in value)
        for item in normalized:
            _nonblank(item, "success_content_types")
        _unique(normalized, "success_content_types")
        return normalized

    @model_validator(mode="after")
    def _endpoint_safe(self) -> EndpointConfig:
        split = urlsplit(
            self.base_url if "://" in self.base_url else f"https://{self.base_url}"
        )
        if split.scheme not in {"https", "http"}:
            raise ValueError(
                "base_url scheme must be https or explicitly allowed local http"
            )
        if split.username or split.password:
            raise ValueError("base_url must not contain userinfo")
        if split.query or split.fragment:
            raise ValueError("base_url must not contain query strings or fragments")
        if not split.netloc:
            raise ValueError("base_url must include a host")
        if split.path.rstrip("/").endswith(_CHAT_COMPLETIONS_SUFFIX):
            raise ValueError("base_url must not include /chat/completions")
        if split.scheme == "http" and not (
            self.allow_insecure_local
            and split.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("http base_url is allowed only for explicit local testing")
        normalized = urlunsplit(
            (split.scheme, split.netloc, split.path.rstrip("/"), "", "")
        )
        object.__setattr__(self, "base_url", normalized)
        return self

    @property
    def chat_completions_url(self) -> str:
        """Return the exact Chat Completions URL for this API prefix."""
        return f"{self.base_url}{_CHAT_COMPLETIONS_SUFFIX}"


class HeaderValuePolicy(BaseModel):
    """Configured non-secret headers admitted for transport requests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    values: dict[str, str] = Field(default_factory=dict)

    @field_validator("values")
    @classmethod
    def _headers_safe(cls, value: dict[str, str]) -> dict[str, str]:
        seen: set[str] = set()
        for name, header_value in value.items():
            normalized = _nonblank(name, "header name").lower()
            if normalized in seen:
                raise ValueError("header names must be unique case-insensitively")
            seen.add(normalized)
            if normalized in _FORBIDDEN_CUSTOM_AUTH_HEADERS:
                raise ValueError(f"configured header {name!r} is protected")
            _nonblank(header_value, "header value")
        return dict(value)


class AuthenticationPolicy(BaseModel):
    """Secret-safe internal authentication policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scheme: AuthenticationScheme
    secret_ref: SecretRef | None = None
    header_name: str | None = None
    allowed_custom_header_names: tuple[str, ...] = ()

    @field_validator("header_name")
    @classmethod
    def _header_name_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _nonblank(value, "header_name")

    @field_validator("allowed_custom_header_names")
    @classmethod
    def _custom_names_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _nonblank(item, "allowed_custom_header_names").lower() for item in value
        )
        _unique(normalized, "allowed_custom_header_names")
        if any(item in _FORBIDDEN_CUSTOM_AUTH_HEADERS for item in normalized):
            raise ValueError("allowed custom authentication header is protected")
        return normalized

    @model_validator(mode="after")
    def _auth_consistent(self) -> AuthenticationPolicy:
        if self.scheme is AuthenticationScheme.NONE:
            if self.secret_ref is not None or self.header_name is not None:
                raise ValueError(
                    "none authentication must not configure a secret or header"
                )
        elif self.scheme is AuthenticationScheme.BEARER:
            if self.secret_ref is None:
                raise ValueError("bearer authentication requires secret_ref")
            if self.header_name is not None:
                raise ValueError("bearer authentication always uses Authorization")
        else:
            if self.secret_ref is None or self.header_name is None:
                raise ValueError(
                    "header authentication requires secret_ref and header_name"
                )
            normalized = self.header_name.lower()
            if normalized in _FORBIDDEN_CUSTOM_AUTH_HEADERS:
                raise ValueError("custom authentication header is protected")
            if normalized not in self.allowed_custom_header_names:
                raise ValueError("custom authentication header must be allowlisted")
        return self


class SamplingPolicy(BaseModel):
    """Resolved default sampling policy and request override allowlist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    seed: int | None = None
    stop: tuple[str, ...] | None = None
    allowed_overrides: tuple[
        str,
        ...,
    ] = (
        "temperature",
        "top_p",
        "presence_penalty",
        "frequency_penalty",
        "seed",
        "stop",
    )
    allow_maximum_output_tokens_override: bool = True

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        copied = dict(data)
        defaults = copied.pop("defaults", None)
        copied.pop("default_maximum_output_tokens", None)
        if defaults is not None:
            default_values = (
                defaults.model_dump(exclude_none=True)
                if isinstance(defaults, SamplingRequest)
                else SamplingRequest.model_validate(defaults).model_dump(
                    exclude_none=True
                )
            )
            for name in _DIRECT_SAMPLING_BODY_FIELDS:
                if name in default_values and name not in copied:
                    copied[name] = default_values[name]
        return copied

    @field_validator("allowed_overrides")
    @classmethod
    def _allowed_overrides_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        allowed = set(_DIRECT_SAMPLING_BODY_FIELDS)
        for item in value:
            if item not in allowed:
                raise ValueError(f"unknown sampling override {item!r}")
        _unique(value, "allowed_overrides")
        return value

    @field_validator("stop")
    @classmethod
    def _stop_values_nonblank(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        for item in value:
            _nonblank(item, "stop")
        return value


class ReasoningPolicy(BaseModel):
    """Provider-neutral reasoning intent with configured wire mappings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: ReasoningMode = ReasoningMode.DISABLED
    effort: ReasoningEffort | None = None
    mode_field: str | None = None
    effort_field: str | None = None
    mode_values: dict[ReasoningMode, JsonValue] = Field(default_factory=dict)
    effort_values: dict[ReasoningEffort, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_reasoning(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        copied = dict(data)
        support = copied.pop("support", None)
        mode_parameter = copied.pop("mode_parameter", None)
        effort_parameter = copied.pop("effort_parameter", None)
        allowed_modes = tuple(copied.pop("allowed_modes", ()) or ())
        allowed_efforts = tuple(copied.pop("allowed_efforts", ()) or ())
        if support is not None and "mode" not in copied:
            support_value = getattr(support, "value", support)
            copied["mode"] = (
                ReasoningMode.DISABLED
                if support_value == ReasoningSupport.UNSUPPORTED.value
                else ReasoningMode.REQUIRED
                if support_value == ReasoningSupport.REQUIRED.value
                else ReasoningMode.ENABLED
            )
        if mode_parameter is not None and "mode_field" not in copied:
            copied["mode_field"] = mode_parameter
        if effort_parameter is not None and "effort_field" not in copied:
            copied["effort_field"] = effort_parameter
        if allowed_modes and "mode_values" not in copied:
            copied["mode_values"] = {
                ReasoningMode.ENABLED: allowed_modes[0],
                ReasoningMode.REQUIRED: allowed_modes[0],
            }
        if allowed_efforts and "effort_values" not in copied:
            effort_values: dict[ReasoningEffort, str] = {}
            for index, item in enumerate(allowed_efforts):
                try:
                    effort = ReasoningEffort(item)
                except ValueError:
                    effort_keys = tuple(ReasoningEffort)
                    if index >= len(effort_keys):
                        break
                    effort = effort_keys[index]
                effort_values[effort] = item
            copied["effort_values"] = effort_values
        if allowed_efforts and "effort" not in copied:
            try:
                copied["effort"] = ReasoningEffort(allowed_efforts[0])
            except ValueError:
                copied["effort"] = ReasoningEffort.LOW
        return copied

    @model_validator(mode="after")
    def _reasoning_mapping_consistent(self) -> ReasoningPolicy:
        if self.mode is not ReasoningMode.DISABLED and self.mode_field is not None:
            if self.mode not in self.mode_values:
                raise ValueError("reasoning mode needs a configured wire value")
        if self.effort is not None:
            if self.effort_field is None:
                raise ValueError("reasoning effort needs a configured wire field")
            if self.effort not in self.effort_values:
                raise ValueError("reasoning effort needs a configured wire value")
        return self


class CapabilityDeclarations(BaseModel):
    """Resolved tri-state capability declarations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    support: dict[str, CapabilitySupport] = Field(default_factory=dict)

    @field_validator("support")
    @classmethod
    def _capabilities_valid(
        cls, value: dict[str, CapabilitySupport]
    ) -> dict[str, CapabilitySupport]:
        for name in value:
            _nonblank(name, "capability name")
        return dict(value)

    def state_for(self, capability_name: str) -> CapabilitySupport:
        """Return the explicit support state or ``unknown`` when absent."""
        return self.support.get(capability_name, CapabilitySupport.UNKNOWN)


class RequestOptionAllowlist(BaseModel):
    """Provider-neutral extra request option allowlist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_options: tuple[str, ...] = ()

    @field_validator("allowed_options")
    @classmethod
    def _options_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        allowed = {"tool_choice", "parallel_tool_calls", "response_format", "user"}
        protected = {
            "model",
            "messages",
            "tools",
            "stream",
            "endpoint",
            "authentication",
            "timeout",
            "host",
            "headers",
            "content_type",
            "user_agent",
            "max_tokens",
            "maximum_output_tokens",
            "temperature",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "seed",
            "stop",
        }
        for item in value:
            _nonblank(item, "allowed_options")
            if item not in allowed and item not in protected:
                raise ValueError(f"request option {item!r} is not supported")
            if item in protected:
                raise ValueError(f"request option {item!r} is protected")
        _unique(value, "allowed_options")
        return value


class ErrorFieldMappings(BaseModel):
    """Immutable provider error field mapping diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id_paths: tuple[str, ...] = ()
    message_paths: tuple[str, ...] = ()
    code_paths: tuple[str, ...] = ()

    @field_validator("request_id_paths", "message_paths", "code_paths")
    @classmethod
    def _paths_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _nonblank(item, "error field path")
            if any(part in {"", "__class__", "__dict__"} for part in item.split(".")):
                raise ValueError("error field paths must be simple dot paths")
        _unique(value, "error field paths")
        return value


class TransportConfig(BaseModel):
    """Internal transport safety configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: float = Field(default=60.0, gt=0)
    success_body_limit_bytes: int = Field(default=4 * 1024 * 1024, gt=0)
    error_body_limit_bytes: int = Field(default=64 * 1024, gt=0)
    follow_redirects: bool = False
    trust_env: bool = False
    tls_verify: bool = True

    @model_validator(mode="after")
    def _transport_safe_defaults(self) -> TransportConfig:
        if not math.isfinite(self.timeout_seconds):
            raise ValueError("timeout_seconds must be finite")
        if self.follow_redirects:
            raise ValueError("model transport must not follow redirects")
        if self.trust_env:
            raise ValueError("model transport must not trust environment proxies")
        if not self.tls_verify:
            raise ValueError("model transport must verify HTTPS TLS by default")
        return self


class ResolvedModelProfile(BaseModel):
    """Immutable provider-neutral resolved model profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str
    provider_id: str
    model_id: str
    transport_id: str = "openai-chat-completions"
    endpoint: EndpointConfig
    authentication: AuthenticationPolicy
    timeout_seconds: float = Field(default=60.0, gt=0)
    maximum_output_tokens: int = Field(default=4096, gt=0)
    configured_headers: HeaderValuePolicy = Field(default_factory=HeaderValuePolicy)
    sampling: SamplingPolicy = Field(default_factory=SamplingPolicy)
    reasoning: ReasoningPolicy = Field(default_factory=ReasoningPolicy)
    capabilities: CapabilityDeclarations = Field(default_factory=CapabilityDeclarations)
    request_options: RequestOptionAllowlist = Field(
        default_factory=RequestOptionAllowlist
    )
    error_mappings: ErrorFieldMappings = Field(default_factory=ErrorFieldMappings)
    transport: TransportConfig = Field(default_factory=TransportConfig)
    source_name: str = "static"
    source_digest: str

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_profile_shape(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        copied = dict(data)
        sampling = copied.get("sampling")
        if "maximum_output_tokens" not in copied and isinstance(sampling, dict):
            legacy_max = sampling.get("default_maximum_output_tokens")
            if legacy_max is not None:
                copied["maximum_output_tokens"] = legacy_max
        return copied

    @field_validator(
        "profile_id",
        "provider_id",
        "model_id",
        "transport_id",
        "source_name",
        "source_digest",
    )
    @classmethod
    def _strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("timeout_seconds must be finite")
        return value

    @property
    def diagnostics(self) -> dict[str, str]:
        """Return sanitized profile source diagnostics only."""
        return {"source_name": self.source_name, "source_digest": self.source_digest}


AuthenticationConfig = AuthenticationPolicy
ModelCapabilities = CapabilityDeclarations
RequestOptions = RequestOptionAllowlist


class TransportRequest(BaseModel):
    """Private transport request record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    profile: ResolvedModelProfile
    public_request: ModelCompletionRequest
    url: str
    headers: dict[str, str]
    body: JsonObject
    timeout_seconds: float = Field(gt=0)

    @field_validator("request_id", "url")
    @classmethod
    def _request_strings_nonblank(cls, value: str, info: Any) -> str:
        return _nonblank(value, info.field_name)


class TransportResponse(BaseModel):
    """Private transport response record after bounded parsing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status_code: int = Field(ge=100, le=599)
    provider_request_id: str | None = None
    body: JsonObject = Field(default_factory=dict)
    normalized_response: ModelCompletionResponse | None = None


class ResolvedSecret:
    """Non-Pydantic wrapper for resolved raw secret values."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise SecretResolutionError("resolved secret must not be empty")
        self._value = value

    def __repr__(self) -> str:
        return "ResolvedSecret(**redacted**)"

    def __str__(self) -> str:
        return "**redacted**"

    def reveal_for_header(self) -> str:
        """Expose the raw value at the authentication header construction point."""
        return self._value


@runtime_checkable
class SecretResolver(Protocol):
    """Resolve an admitted secret reference without exposing raw values."""

    def resolve(self, ref: SecretRef) -> ResolvedSecret:
        """Resolve ``ref`` to a non-serializable secret wrapper."""
        ...


@runtime_checkable
class ModelProfileResolver(Protocol):
    """Resolve a logical profile ID into an immutable internal profile."""

    def resolve(self, profile_id: str) -> ResolvedModelProfile:
        """Resolve ``profile_id`` exactly and without network I/O."""
        ...

    def diagnostics(self, profile_id: str) -> dict[str, str]:
        """Return sanitized source diagnostics for a known profile."""
        ...


@runtime_checkable
class ModelTransport(Protocol):
    """Private model transport boundary."""

    async def send(self, request: TransportRequest) -> TransportResponse:
        """Send one already-normalized transport request."""
        ...


@runtime_checkable
class ModelCancellationToken(Protocol):
    """Minimal cancellation token shape consumed by the default client."""

    @property
    def cancellation_id(self) -> str:
        """Return the cancellation identifier."""
        ...

    def is_cancelled(self) -> bool:
        """Return whether cancellation has been requested."""
        ...

    @property
    def reason(self) -> str | None:
        """Return a bounded cancellation reason, when present."""
        ...


@runtime_checkable
class ModelCancellationResolver(Protocol):
    """Resolve a public cancellation reference for backend checks."""

    def resolve(self, ref: Any) -> ModelCancellationToken:
        """Resolve a cancellation reference into a checkable token."""
        ...


@runtime_checkable
class ModelBackendClock(Protocol):
    """Clock shape needed for deadline checks."""

    def monotonic(self) -> float:
        """Return monotonic seconds."""
        ...


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()


class StaticModelProfileResolver:
    """In-process exact profile resolver used by composition roots and tests."""

    def __init__(self, profiles: Mapping[str, ResolvedModelProfile]) -> None:
        copied = dict(profiles)
        for profile_id, profile in copied.items():
            if profile_id != profile.profile_id:
                raise ModelBackendConfigError(
                    "profile mapping key must match profile_id"
                )
        self._profiles = copied

    def resolve(self, profile_id: str) -> ResolvedModelProfile:
        """Return an immutable profile or reject before secret/transport work."""
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise ModelBackendConfigError(
                f"unknown model profile {redact_text(profile_id)!r}"
            ) from exc

    def diagnostics(self, profile_id: str) -> dict[str, str]:
        """Return only sanitized profile source diagnostics."""
        return self.resolve(profile_id).diagnostics


class StaticSecretResolver:
    """Small deterministic resolver for tests and local composition."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def resolve(self, ref: SecretRef) -> ResolvedSecret:
        try:
            return ResolvedSecret(self._values[ref.secret_id])
        except KeyError as exc:
            raise SecretResolutionError(f"missing secret {ref.secret_id!r}") from exc


class CapabilityNegotiator:
    """Evaluate public capability requirements against a resolved profile."""

    _FIELDS = tuple(ModelCapabilityRequirements.model_fields)

    def negotiate(
        self,
        required: ModelCapabilityRequirements,
        declarations: CapabilityDeclarations,
    ) -> None:
        """Raise stable provider-neutral failure data for unsupported requirements."""
        failures: dict[str, SanitizedMetadataValue] = {}
        for field_name in self._FIELDS:
            if getattr(required, field_name) is True:
                state = declarations.state_for(field_name)
                if state is not CapabilitySupport.SUPPORTED:
                    failures[field_name] = state.value
        if failures:
            raise ModelProviderError(
                category=ProviderErrorCategory.UNSUPPORTED_CAPABILITY,
                message="required model capabilities are unsupported",
                retryable=False,
                fields=failures,
            )


class OpenAIChatCompletionsTransport:
    """Non-streaming OpenAI-compatible Chat Completions HTTP transport."""

    def __init__(
        self,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        timeout = _phase_timeout(timeout_seconds)
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            verify=True,
            timeout=timeout,
            transport=http_transport,
        )
        self._client.headers.clear()
        self._closed = False

    async def __aenter__(self) -> OpenAIChatCompletionsTransport:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the owned HTTP client at most once."""
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()

    async def send(self, request: TransportRequest) -> TransportResponse:
        """POST one bounded non-streaming Chat Completions request."""
        try:
            response = await self._client.post(
                request.url,
                headers=request.headers,
                json=request.body,
                timeout=_phase_timeout(request.timeout_seconds),
            )
        except httpx.TimeoutException as exc:
            raise ModelProviderError(
                category=ProviderErrorCategory.TIMEOUT,
                message=f"model transport timed out for {redact_url(request.url)}",
            ) from exc
        except httpx.NetworkError as exc:
            raise ModelProviderError(
                category=ProviderErrorCategory.CONNECTION,
                message=f"model transport connection failed for {redact_url(request.url)}",
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                category=ProviderErrorCategory.UNKNOWN,
                message=f"model transport failed for {redact_url(request.url)}",
            ) from exc

        provider_request_id = _provider_request_id(
            request.profile.error_mappings, response.headers, None
        )
        if response.status_code >= 400:
            body = await _read_limited_response(
                response, request.profile.transport.error_body_limit_bytes
            )
            parsed = _decode_json_body(body, error_body=True)
            provider_request_id = _provider_request_id(
                request.profile.error_mappings, response.headers, parsed
            )
            raise _provider_http_error(
                response.status_code, parsed, provider_request_id
            )

        raw_content_type = response.headers.get("content-type")
        content_type = (
            None
            if raw_content_type is None
            else raw_content_type.split(";", 1)[0].strip().lower()
        )
        if content_type is None:
            if not request.profile.endpoint.allow_missing_success_content_type:
                raise _malformed_response("provider response content type is invalid")
        elif content_type not in request.profile.endpoint.success_content_types:
            raise _malformed_response("provider response content type is invalid")
        body = await _read_limited_response(
            response, request.profile.transport.success_body_limit_bytes
        )
        parsed = _decode_json_body(body, error_body=False)
        provider_request_id = _provider_request_id(
            request.profile.error_mappings, response.headers, parsed
        )
        transport_response = TransportResponse(
            status_code=response.status_code,
            provider_request_id=provider_request_id,
            body=parsed,
        )
        normalized = normalize_transport_response(request.profile, transport_response)
        return transport_response.model_copy(update={"normalized_response": normalized})


class DefaultModelClient:
    """Default provider-neutral model client orchestration.

    The client owns backend policy checks and delegates exactly one already-built
    request to the configured private transport.
    """

    def __init__(
        self,
        *,
        profile_resolver: ModelProfileResolver,
        secret_resolver: SecretResolver,
        transport: ModelTransport,
        cancellation_resolver: ModelCancellationResolver | None = None,
        clock: ModelBackendClock | None = None,
        capability_negotiator: CapabilityNegotiator | None = None,
    ) -> None:
        self._profile_resolver = profile_resolver
        self._secret_resolver = secret_resolver
        self._transport = transport
        self._cancellation_resolver = cancellation_resolver
        self._clock = clock or _SystemClock()
        self._capability_negotiator = capability_negotiator or CapabilityNegotiator()
        self._closed = False

    async def __aenter__(self) -> DefaultModelClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close owned transport resources at most once."""
        if self._closed:
            return
        self._closed = True
        close = getattr(self._transport, "aclose", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    async def complete(
        self, request: ModelCompletionRequest
    ) -> ModelCompletionResponse:
        """Resolve backend policy, call transport once, and normalize the response."""
        profile = self._profile_resolver.resolve(request.model_profile_id)
        if profile.profile_id != request.model_profile_id:
            raise ModelBackendConfigError("resolved profile identity mismatch")

        self._capability_negotiator.negotiate(
            request.required_capabilities, profile.capabilities
        )
        validate_reasoning_policy(profile.reasoning)
        sampling = merge_sampling_policy(profile.sampling, request.sampling_overrides)
        maximum_output_tokens = merge_maximum_output_tokens(
            profile, request.maximum_output_tokens_override
        )
        body = build_transport_body(
            profile,
            request,
            sampling,
            maximum_output_tokens,
        )

        token = self._resolve_cancellation(request)
        self._check_cancelled(token)
        timeout_seconds = min(
            profile.timeout_seconds, self._remaining_timeout_seconds(request)
        )
        self._validate_secret_refs(profile.authentication, request.secret_refs)
        resolved_secret = resolve_authentication_secret(
            profile.authentication,
            request.secret_refs,
            self._secret_resolver,
        )
        self._check_cancelled(token)

        transport_request = TransportRequest(
            request_id=request.request_id,
            profile=profile,
            public_request=request,
            url=profile.endpoint.chat_completions_url,
            headers=self._headers(profile, resolved_secret),
            body=body,
            timeout_seconds=timeout_seconds,
        )
        transport_response = await self._transport.send(transport_request)
        self._check_cancelled(token)
        self._remaining_timeout_seconds(request)
        return normalize_transport_response(profile, transport_response)

    def _resolve_cancellation(
        self, request: ModelCompletionRequest
    ) -> ModelCancellationToken | None:
        if self._cancellation_resolver is None:
            return None
        return self._cancellation_resolver.resolve(request.cancellation)

    def _check_cancelled(self, token: ModelCancellationToken | None) -> None:
        if token is None or not token.is_cancelled():
            return
        reason = redact_text(token.reason or "model request cancelled")
        raise ModelProviderError(
            category=ProviderErrorCategory.CANCELLED,
            message=reason,
            retryable=False,
        )

    def _remaining_timeout_seconds(self, request: ModelCompletionRequest) -> float:
        remaining = request.deadline.remaining(self._clock)
        if remaining <= 0:
            raise ModelProviderError(
                category=ProviderErrorCategory.TIMEOUT,
                message="model request deadline expired",
                retryable=False,
            )
        return remaining

    def _headers(
        self,
        profile: ResolvedModelProfile,
        resolved_secret: ResolvedSecret | None,
    ) -> dict[str, str]:
        return assemble_transport_headers(profile, resolved_secret)

    def _validate_secret_refs(
        self,
        authentication: AuthenticationPolicy,
        admitted: tuple[SecretRef, ...],
    ) -> None:
        secret_ids: set[str] = set()
        env_vars: set[str] = set()
        for secret in admitted:
            if secret.secret_id in secret_ids:
                raise SecretResolutionError(
                    "duplicate secret_id in request secret_refs"
                )
            if secret.env_var in env_vars:
                raise SecretResolutionError("duplicate env_var in request secret_refs")
            secret_ids.add(secret.secret_id)
            env_vars.add(secret.env_var)

        configured = authentication.secret_ref
        if configured is None:
            if admitted:
                raise SecretResolutionError("unknown secret reference was admitted")
            return
        if len(admitted) != 1:
            raise SecretResolutionError("exactly one authentication secret is required")
        if admitted[0] != configured:
            raise SecretResolutionError("admitted secret does not match configuration")


def merge_sampling_policy(
    policy: SamplingPolicy,
    overrides: SamplingRequest,
) -> SamplingRequest:
    """Merge sampling defaults with explicitly allowlisted caller overrides."""
    merged = {
        "temperature": policy.temperature,
        "top_p": policy.top_p,
        "presence_penalty": policy.presence_penalty,
        "frequency_penalty": policy.frequency_penalty,
        "seed": policy.seed,
        "stop": policy.stop,
    }
    override_values = overrides.model_dump(exclude_none=True)
    for name, value in override_values.items():
        if name not in _DIRECT_SAMPLING_BODY_FIELDS:
            raise ModelBackendConfigError(f"sampling override {name!r} is not allowed")
        if name not in policy.allowed_overrides:
            raise ModelBackendConfigError(f"sampling override {name!r} is not allowed")
        merged[name] = value
    return SamplingRequest.model_validate(merged)


def merge_maximum_output_tokens(
    profile: ResolvedModelProfile,
    override: int | None,
) -> int:
    """Return the effective output-token cap under profile override policy."""
    if override is None:
        return profile.maximum_output_tokens
    if not profile.sampling.allow_maximum_output_tokens_override:
        raise ModelBackendConfigError("maximum output token override is not allowed")
    return override


def build_transport_body(
    profile: ResolvedModelProfile,
    request: ModelCompletionRequest,
    sampling: SamplingRequest,
    maximum_output_tokens: int | None,
) -> JsonObject:
    """Build the provider-neutral request body consumed by private transports."""
    body: JsonObject = {
        "model": profile.model_id,
        "messages": [_message_payload(message) for message in request.messages],
        "stream": False,
    }
    if request.tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in request.tools
        ]
    for name, value in sampling.model_dump(
        include=_DIRECT_SAMPLING_BODY_FIELDS,
        exclude_none=True,
    ).items():
        body[name] = value
    if maximum_output_tokens is not None:
        body["max_tokens"] = maximum_output_tokens
    _add_reasoning_controls(profile, sampling, body)
    _add_request_options(profile, request, body)
    return body


def validate_reasoning_policy(policy: ReasoningPolicy) -> None:
    """Reject un-mappable required reasoning before secret resolution and HTTP."""
    if policy.mode is not ReasoningMode.REQUIRED:
        return
    if policy.mode_field is None or policy.mode not in policy.mode_values:
        raise ModelBackendConfigError("required reasoning has no faithful mapping")


def _add_reasoning_controls(
    profile: ResolvedModelProfile,
    sampling: SamplingRequest,
    body: JsonObject,
) -> None:
    del sampling
    if (
        profile.reasoning.mode is not ReasoningMode.DISABLED
        and profile.reasoning.mode_field is not None
    ):
        body[profile.reasoning.mode_field] = profile.reasoning.mode_values[
            profile.reasoning.mode
        ]
    if profile.reasoning.effort is not None:
        if profile.reasoning.effort_field is None:
            raise ModelBackendConfigError("reasoning effort has no configured mapping")
        body[profile.reasoning.effort_field] = profile.reasoning.effort_values[
            profile.reasoning.effort
        ]


def _add_request_options(
    profile: ResolvedModelProfile,
    request: ModelCompletionRequest,
    body: JsonObject,
) -> None:
    allowed = set(profile.request_options.allowed_options)
    for name, value in request.request_options.items():
        if name not in allowed:
            raise ModelBackendConfigError(f"request option {name!r} is not allowed")
        body[name] = value


def assemble_transport_headers(
    profile: ResolvedModelProfile,
    resolved_secret: ResolvedSecret | None,
) -> dict[str, str]:
    """Assemble headers in stable base/configured/auth order."""
    headers: dict[str, str] = {}
    _append_header(headers, "Content-Type", "application/json")
    _append_header(headers, "User-Agent", "millforge-model-backend/1")
    for name, value in profile.configured_headers.values.items():
        _append_header(headers, name, value)
    for name, value in build_auth_headers(
        profile.authentication, resolved_secret
    ).items():
        _append_header(headers, name, value)
    return headers


def _append_header(headers: dict[str, str], name: str, value: str) -> None:
    normalized = name.lower()
    if any(existing.lower() == normalized for existing in headers):
        raise ModelBackendConfigError(
            f"duplicate transport header {redact_text(name)!r}"
        )
    headers[name] = value


def _message_payload(message: ModelMessage) -> JsonObject:
    if message.role == "assistant":
        payload: JsonObject = {"role": "assistant"}
        if message.content is not None:
            payload["content"] = message.content
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": _tool_arguments_json(call),
                    },
                }
                for call in message.tool_calls
            ]
        return payload
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "name": message.tool_name,
            "content": message.content,
        }
    return {"role": message.role, "content": message.content}


def _tool_arguments_json(call: ModelToolCall) -> str:
    if isinstance(call.arguments, ParsedToolArguments):
        _reject_non_finite_json(call.arguments.value, path="tool arguments")
        return json.dumps(call.arguments.value, sort_keys=True, separators=(",", ":"))
    return str(call.arguments.raw)


def _reject_non_finite_json(value: JsonValue, *, path: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ModelBackendConfigError(f"{path} contains non-finite numeric value")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_non_finite_json(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_non_finite_json(item, path=f"{path}[{index}]")


def normalize_transport_response(
    profile: ResolvedModelProfile,
    response: TransportResponse,
) -> ModelCompletionResponse:
    """Return only owned response data from a transport response."""
    if response.normalized_response is not None:
        normalized = ModelCompletionResponse.model_validate(
            response.normalized_response.model_dump()
        )
        if normalized.model_id != profile.model_id:
            raise ModelProviderError(
                category=ProviderErrorCategory.MALFORMED_RESPONSE,
                message="provider response model did not match resolved profile",
                retryable=False,
            )
        if normalized.provider_request_id is None and response.provider_request_id:
            normalized = normalized.model_copy(
                update={"provider_request_id": response.provider_request_id}
            )
        return normalized
    return _normalize_openai_chat_body(profile, response)


def _normalize_openai_chat_body(
    profile: ResolvedModelProfile,
    response: TransportResponse,
) -> ModelCompletionResponse:
    body = response.body
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise _malformed_response("provider response requires exactly one choice")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise _malformed_response("provider response choice is malformed")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise _malformed_response("provider response message is malformed")
    if message.get("role") != "assistant":
        raise _malformed_response("provider response message role is malformed")

    model_id = body.get("model", profile.model_id)
    if model_id != profile.model_id:
        raise _malformed_response("provider response model did not match profile")

    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise _malformed_response("provider response content is malformed")
    assistant = AssistantMessage(
        content=content,
        tool_calls=_normalize_tool_calls(message.get("tool_calls", ())),
    )
    finish_reason = _FINISH_REASON_MAP.get(str(choice.get("finish_reason")), "unknown")
    usage = _normalize_usage(body.get("usage"))
    return ModelCompletionResponse(
        provider_request_id=response.provider_request_id,
        model_id=profile.model_id,
        message=assistant,
        finish_reason=cast(
            Any,
            finish_reason,
        ),
        usage=usage,
    )


def _normalize_tool_calls(value: object) -> tuple[ModelToolCall, ...]:
    if value in (None, ()):
        return ()
    if not isinstance(value, list):
        raise _malformed_response("provider response tool_calls is malformed")
    calls: list[ModelToolCall] = []
    seen_call_ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise _malformed_response("provider response tool call is malformed")
        function = item.get("function")
        if not isinstance(function, dict):
            raise _malformed_response("provider response tool function is malformed")
        if item.get("type") not in (None, "function"):
            raise _malformed_response("provider response tool call type is malformed")
        call_id = item.get("id")
        name = function.get("name")
        if not isinstance(call_id, str) or not isinstance(name, str):
            raise _malformed_response(
                "provider response tool call identity is malformed"
            )
        if call_id in seen_call_ids:
            raise _malformed_response("provider response tool call IDs are duplicated")
        seen_call_ids.add(call_id)
        raw_arguments = function.get("arguments", "")
        calls.append(
            ModelToolCall(
                call_id=call_id,
                name=name,
                arguments=_normalize_tool_arguments(raw_arguments),
            )
        )
    return tuple(calls)


def _normalize_tool_arguments(
    raw: JsonValue,
) -> ParsedToolArguments | InvalidToolArguments:
    if isinstance(raw, dict):
        return ParsedToolArguments(value=raw)
    if not isinstance(raw, str):
        return InvalidToolArguments(raw=raw, error_code="not_json_object")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return InvalidToolArguments(raw=raw, error_code="malformed_json")
    if not isinstance(parsed, dict):
        return InvalidToolArguments(raw=raw, error_code="not_json_object")
    return ParsedToolArguments(value=parsed)


def _normalize_usage(value: object) -> TokenUsage | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _malformed_response("provider usage is malformed")
    input_tokens = value.get("prompt_tokens")
    output_tokens = value.get("completion_tokens")
    total_tokens = value.get("total_tokens")
    if not isinstance(input_tokens, int):
        raise _malformed_response("provider usage token counts are malformed")
    if not isinstance(output_tokens, int):
        raise _malformed_response("provider usage token counts are malformed")
    if not isinstance(total_tokens, int):
        raise _malformed_response("provider usage token counts are malformed")
    try:
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            provider_reported=True,
        )
    except ValueError as exc:
        raise _malformed_response(
            "provider usage token counts are inconsistent"
        ) from exc


def _malformed_response(message: str) -> ModelProviderError:
    return ModelProviderError(
        category=ProviderErrorCategory.MALFORMED_RESPONSE,
        message=message,
        retryable=False,
    )


async def _read_limited_response(
    response: httpx.Response,
    limit_bytes: int,
) -> bytes:
    body = await response.aread()
    if len(body) > limit_bytes:
        raise ModelProviderError(
            category=ProviderErrorCategory.MALFORMED_RESPONSE,
            message="provider response body exceeded configured limit",
            retryable=False,
        )
    return body


def _decode_json_body(body: bytes, *, error_body: bool) -> JsonObject:
    if not body:
        return {}
    try:
        parsed = json.loads(body, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        if error_body:
            return {}
        raise ModelProviderError(
            category=ProviderErrorCategory.MALFORMED_RESPONSE,
            message="provider response body was not valid JSON",
            retryable=False,
        ) from exc
    except ValueError as exc:
        raise ModelProviderError(
            category=ProviderErrorCategory.MALFORMED_RESPONSE,
            message="provider response JSON contained duplicate keys",
            retryable=False,
        ) from exc
    if not isinstance(parsed, dict):
        raise _malformed_response("provider response JSON must be an object")
    return cast(JsonObject, parsed)


def _reject_duplicate_json_keys(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _provider_http_error(
    status_code: int,
    body: JsonObject,
    provider_request_id: str | None,
) -> ModelProviderError:
    category = _category_for_status(status_code, body)
    message = _error_message(body) or f"provider returned HTTP {status_code}"
    fields: dict[str, SanitizedMetadataValue] = {"status_code": status_code}
    code = _dot_path(body, "error.code")
    if isinstance(code, str):
        fields["provider_code"] = code
    raise ModelProviderError(
        category=category,
        message=message,
        provider_request_id=provider_request_id,
        fields=fields,
    )


def _phase_timeout(timeout_seconds: float) -> httpx.Timeout:
    if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
        raise ModelBackendConfigError("transport timeout must be positive and finite")
    return httpx.Timeout(
        timeout_seconds,
        connect=timeout_seconds,
        read=timeout_seconds,
        write=timeout_seconds,
        pool=timeout_seconds,
    )


def _category_for_status(
    status_code: int,
    body: JsonObject,
) -> ProviderErrorCategory:
    code = _dot_path(body, "error.code")
    text = str(code).lower() if code is not None else ""
    if status_code == 401:
        return ProviderErrorCategory.AUTHENTICATION
    if status_code == 403:
        return ProviderErrorCategory.AUTHORIZATION
    if status_code == 408:
        return ProviderErrorCategory.TIMEOUT
    if status_code == 429:
        return ProviderErrorCategory.RATE_LIMIT
    if status_code in {400, 409, 422}:
        return ProviderErrorCategory.INVALID_REQUEST
    if status_code == 501 or "unsupported" in text:
        return ProviderErrorCategory.UNSUPPORTED_CAPABILITY
    if status_code >= 500:
        return ProviderErrorCategory.SERVER_ERROR
    return ProviderErrorCategory.UNKNOWN


def _error_message(body: JsonObject) -> str | None:
    message = _dot_path(body, "error.message")
    if isinstance(message, str) and message.strip():
        return message
    return None


def _provider_request_id(
    mappings: ErrorFieldMappings,
    headers: httpx.Headers,
    body: JsonObject | None,
) -> str | None:
    for header_name in ("x-request-id", "request-id", "openai-request-id"):
        value = headers.get(header_name)
        if value:
            return _bounded(redact_text(value), length=128)
    if body is None:
        return None
    for path in mappings.request_id_paths:
        value = _dot_path(body, path)
        if isinstance(value, str) and value.strip():
            return _bounded(redact_text(value), length=128)
    return None


def _dot_path(body: Mapping[str, JsonValue], path: str) -> JsonValue:
    current: JsonValue = body
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def assert_secret_admitted(
    configured: SecretRef, admitted: tuple[SecretRef, ...]
) -> None:
    """Ensure a configured secret reference is present in the public request."""
    if configured not in admitted:
        raise SecretResolutionError(f"secret {configured.secret_id!r} was not admitted")


def resolve_authentication_secret(
    policy: AuthenticationPolicy,
    admitted: tuple[SecretRef, ...],
    resolver: SecretResolver,
) -> ResolvedSecret | None:
    """Resolve a configured auth secret only after admission succeeds."""
    if policy.secret_ref is None:
        return None
    assert_secret_admitted(policy.secret_ref, admitted)
    return resolver.resolve(policy.secret_ref)


def build_auth_headers(
    policy: AuthenticationPolicy,
    resolved_secret: ResolvedSecret | None,
) -> dict[str, str]:
    """Build only authentication headers from an already-resolved secret."""
    if policy.scheme is AuthenticationScheme.NONE:
        return {}
    if resolved_secret is None:
        raise SecretResolutionError("authentication secret was not resolved")
    raw = resolved_secret.reveal_for_header()
    if policy.scheme is AuthenticationScheme.BEARER:
        return {"Authorization": f"Bearer {raw}"}
    if policy.header_name is None:
        raise SecretResolutionError("custom authentication header is missing")
    return {policy.header_name: raw}


def redact_url(value: str) -> str:
    """Redact URL userinfo and query values for diagnostics."""
    split = urlsplit(value)
    if not split.scheme or not split.netloc:
        return _redact_secret_patterns(value)
    host = split.hostname or ""
    if split.port:
        host = f"{host}:{split.port}"
    query = "&".join(
        f"{key}=**redacted**"
        for key, _ in parse_qsl(split.query, keep_blank_values=True)
    )
    return urlunsplit(
        (
            split.scheme,
            host,
            split.path,
            query,
            "**redacted**" if split.fragment else "",
        )
    )


def _redact_secret_patterns(text: str) -> str:
    text = _SECRET_PATTERNS[0].sub(
        lambda match: (
            match.group(0)
            if match.group(2) == "**redacted**"
            else f"{match.group(1)}**redacted**"
        ),
        text,
    )
    for pattern in _SECRET_PATTERNS[1:]:
        text = pattern.sub(lambda match: f"{match.group(1)}**redacted**", text)
    return text


def redact_text(value: object, *, secret_values: tuple[str, ...] = ()) -> str:
    """Redact auth headers, explicit secrets, URL details, and token/key patterns."""
    text = str(value)
    for secret in secret_values:
        if secret:
            text = text.replace(secret, "**redacted**")
    text = _URL_PATTERN.sub(lambda match: redact_url(match.group(0)), text)
    return _redact_secret_patterns(text)


def sanitize_provider_error_fields(
    fields: Mapping[str, SanitizedMetadataValue],
    *,
    secret_values: tuple[str, ...] = (),
) -> dict[str, SanitizedMetadataValue]:
    """Bound and redact provider error fields for safe persistence."""
    sanitized: dict[str, SanitizedMetadataValue] = {}
    for index, (key, value) in enumerate(fields.items()):
        if index >= _MAX_SANITIZED_FIELDS:
            break
        clean_key = _bounded(redact_text(key), length=64)
        lowered = clean_key.lower()
        if any(marker in lowered for marker in _SENSITIVE_FIELD_MARKERS):
            sanitized[clean_key] = "**redacted**"
        elif isinstance(value, str):
            sanitized[clean_key] = _bounded(
                redact_text(value, secret_values=secret_values)
            )
        else:
            sanitized[clean_key] = value
    return sanitized


def redact_mapping(
    values: Mapping[str, object],
    *,
    secret_values: tuple[str, ...] = (),
) -> dict[str, SanitizedMetadataValue]:
    """Redact debug summaries, events, traces, metrics, manifests, and diagnostics."""
    sanitized: dict[str, SanitizedMetadataValue] = {}
    for key, value in values.items():
        lowered = key.lower()
        if any(marker in lowered for marker in _SENSITIVE_FIELD_MARKERS):
            sanitized[key] = "**redacted**"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            sanitized[key] = (
                _bounded(redact_text(value, secret_values=secret_values))
                if isinstance(value, str)
                else value
            )
        else:
            sanitized[key] = _bounded(
                redact_text(repr(value), secret_values=secret_values)
            )
    return sanitized
