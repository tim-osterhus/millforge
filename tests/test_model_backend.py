"""Tests for internal model backend contracts."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from types import MappingProxyType
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from millforge.contracts import (
    AssistantMessage,
    CancellationRef,
    Deadline,
    InvalidToolArguments,
    ModelCapabilityRequirements,
    ModelCompletionRequest,
    ModelCompletionResponse,
    ModelToolCall,
    ModelToolDefinition,
    SamplingRequest,
    SecretRef,
    SystemMessage,
    TokenUsage,
    ToolResultMessage,
    UserMessage,
    ParsedToolArguments,
)
from millforge.model_backend import (
    AuthenticationPolicy,
    AuthenticationScheme,
    CapabilityDeclarations,
    CapabilityNegotiator,
    CapabilitySupport,
    DefaultModelClient,
    EndpointConfig,
    HeaderValuePolicy,
    ModelBackendConfigError,
    ModelProviderError,
    ModelRequestDeadlineExceededError,
    OpenAIChatCompletionsTransport,
    OpenAICompatibleTimeouts,
    ProviderErrorCategory,
    RequestOptionAllowlist,
    ResolvedModelProfile,
    ResolvedSecret,
    ReasoningPolicy,
    ReasoningEffort,
    ReasoningMode,
    SamplingPolicy,
    SecretResolutionError,
    StaticModelProfileResolver,
    StaticSecretResolver,
    TransportConfig,
    TransportRequest,
    TransportResponse,
    build_auth_headers,
    DEFAULT_REDACTION_POLICY,
    redact_mapping,
    redact_text,
    resolve_authentication_secret,
    sanitize_provider_error_fields,
)
from tests.conftest import (
    BUILDER_COMPAT_A_SECRET,
    BUILDER_COMPAT_B_SECRET,
    BUILDER_FIXTURE_PROFILE_ID,
    LIVE_MODEL_BACKEND_ENV_VARS,
    LIVE_MODEL_BACKEND_SMOKE_FLAG,
    live_model_backend_smoke_enabled,
    make_canonical_builder_profile_a,
    make_canonical_builder_profile_b,
)


def _deadline() -> Deadline:
    return Deadline(
        started_monotonic=0.0,
        outer_deadline_monotonic=60.0,
        effective_deadline_monotonic=60.0,
        source="request",
    )


def _request(secret_refs: tuple[SecretRef, ...] = ()) -> ModelCompletionRequest:
    return ModelCompletionRequest(
        request_id="req-1",
        run_id="run-1",
        model_profile_id="profile.openai",
        messages=(UserMessage(content="hello"),),
        deadline=_deadline(),
        cancellation=CancellationRef(cancellation_id="cancel-1"),
        secret_refs=secret_refs,
    )


def _response(*, content: str = "done") -> ModelCompletionResponse:
    return ModelCompletionResponse(
        provider_request_id=None,
        model_id="gpt-test",
        message=AssistantMessage(content=content),
        finish_reason="stop",
        usage=None,
    )


def _secret() -> SecretRef:
    return SecretRef(secret_id="openai-key", env_var="OPENAI_API_KEY")


def _profile() -> ResolvedModelProfile:
    return ResolvedModelProfile(
        profile_id="profile.openai",
        provider_id="openai-compatible",
        model_id="gpt-test",
        endpoint=EndpointConfig(base_url="https://api.example.test/v1/"),
        authentication=AuthenticationPolicy(
            scheme=AuthenticationScheme.BEARER,
            secret_ref=_secret(),
        ),
        capabilities=CapabilityDeclarations(
            support={
                "tool_calls": CapabilitySupport.SUPPORTED,
                "system_messages": CapabilitySupport.SUPPORTED,
                "tool_result_messages": CapabilitySupport.SUPPORTED,
                "parallel_tool_calls": CapabilitySupport.UNSUPPORTED,
                "structured_output": CapabilitySupport.UNSUPPORTED,
                "reasoning_controls": CapabilitySupport.UNSUPPORTED,
                "usage_reporting": CapabilitySupport.UNKNOWN,
            }
        ),
        source_name="unit-test",
        source_digest="digest:abc123",
    )


def _replay_profile(
    *,
    mode: ReasoningMode = ReasoningMode.ENABLED,
    success_body_limit_bytes: int = 4 * 1024 * 1024,
    provider_id: str = "opaque-provider-a",
) -> ResolvedModelProfile:
    return _profile().model_copy(
        update={
            "provider_id": provider_id,
            "reasoning": ReasoningPolicy(
                mode=mode,
                mode_field="thinking",
                mode_values={mode: {"type": "enabled"}},
                tool_call_replay_field="reasoning_content",
            ),
            "transport": _profile().transport.model_copy(
                update={"success_body_limit_bytes": success_body_limit_bytes}
            ),
        }
    )


def _reasoning_tool_call(call_id: str = "provider-call-1") -> ModelToolCall:
    return ModelToolCall(
        call_id=call_id,
        name="lookup",
        arguments=ParsedToolArguments(value={"city": "Hilo"}),
    )


def _raw_reasoning_response(
    reasoning_content: object = "exact continuation",
    *,
    include_reasoning_content: bool = True,
    call_id: str = "provider-call-1",
) -> TransportResponse:
    message: dict[str, object] = {
        "role": "assistant",
        "content": "ordinary assistant content",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "lookup",
                    "arguments": '{"city":"Hilo"}',
                },
            }
        ],
    }
    if include_reasoning_content:
        message["reasoning_content"] = reasoning_content
    return TransportResponse(
        status_code=200,
        body={
            "model": "gpt-test",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": message,
                }
            ],
        },
    )


def _normalized_reasoning_response(
    value: object = "exact continuation",
) -> ModelCompletionResponse:
    message = AssistantMessage.model_construct(
        content="ordinary assistant content",
        tool_calls=(_reasoning_tool_call(),),
        reasoning_content=value,
    )
    return ModelCompletionResponse.model_construct(
        provider_request_id="provider-normalized-1",
        model_id="gpt-test",
        message=message,
        finish_reason="tool_calls",
        usage=None,
        provider_metadata=None,
    )


def _live_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        pytest.skip(f"{name} is required for live_model_backend smoke")
    return value.strip()


class _EnvironmentSecretResolver:
    def resolve(self, ref: SecretRef) -> ResolvedSecret:
        value = os.environ.get(ref.env_var)
        if value is None or not value:
            raise SecretResolutionError(
                f"missing configured secret environment variable {ref.env_var!r}"
            )
        return ResolvedSecret(value)


def _live_model_profile(secret_ref: SecretRef) -> ResolvedModelProfile:
    auth_scheme = AuthenticationScheme(
        os.environ.get("MILLFORGE_LIVE_MODEL_AUTH_SCHEME", "bearer").strip().lower()
    )
    header_name = os.environ.get("MILLFORGE_LIVE_MODEL_AUTH_HEADER")
    authentication = (
        AuthenticationPolicy(
            scheme=AuthenticationScheme.HEADER,
            secret_ref=secret_ref,
            header_name=header_name.strip() if header_name else None,
            allowed_custom_header_names=(
                (header_name.strip().lower(),) if header_name else ()
            ),
        )
        if auth_scheme is AuthenticationScheme.HEADER
        else AuthenticationPolicy(scheme=auth_scheme, secret_ref=secret_ref)
    )

    timeout_seconds = float(
        os.environ.get("MILLFORGE_LIVE_MODEL_TIMEOUT_SECONDS", "20")
    )
    maximum_output_tokens = int(
        os.environ.get("MILLFORGE_LIVE_MODEL_MAX_OUTPUT_TOKENS", "16")
    )
    return ResolvedModelProfile(
        profile_id=_live_env("MILLFORGE_LIVE_MODEL_PROFILE_ID"),
        provider_id=_live_env("MILLFORGE_LIVE_MODEL_PROVIDER_ID"),
        model_id=_live_env("MILLFORGE_LIVE_MODEL_ID"),
        endpoint=EndpointConfig(base_url=_live_env("MILLFORGE_LIVE_MODEL_BASE_URL")),
        authentication=authentication,
        timeout_seconds=timeout_seconds,
        maximum_output_tokens=maximum_output_tokens,
        capabilities=CapabilityDeclarations(
            support={
                "tool_calls": CapabilitySupport.SUPPORTED,
                "system_messages": CapabilitySupport.SUPPORTED,
                "tool_result_messages": CapabilitySupport.SUPPORTED,
                "parallel_tool_calls": CapabilitySupport.UNSUPPORTED,
                "structured_output": CapabilitySupport.UNSUPPORTED,
                "reasoning_controls": CapabilitySupport.UNSUPPORTED,
                "usage_reporting": CapabilitySupport.UNKNOWN,
            }
        ),
        source_name="env-live-smoke",
        source_digest="env:MILLFORGE_LIVE_MODEL_*",
    )


def _usage_report(usage: TokenUsage | None) -> dict[str, int | bool] | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "provider_reported": usage.provider_reported,
    }


def _record_sanitized_smoke_report(
    record_property: Any,
    *,
    profile: ResolvedModelProfile,
    response: ModelCompletionResponse,
    latency_ms: int,
) -> None:
    report = {
        "provider": profile.provider_id,
        "model": response.model_id,
        "latency_ms": latency_ms,
        "finish_reason": response.finish_reason,
        "usage": _usage_report(response.usage),
    }
    for key, value in report.items():
        record_property(f"live_model_backend_{key}", value)


class _RecordingTransport:
    def __init__(self, response: TransportResponse) -> None:
        self.response = response
        self.requests: list[TransportRequest] = []
        self.close_count = 0

    async def send(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        return self.response

    async def aclose(self) -> None:
        self.close_count += 1


class _ControlledTransport:
    def __init__(
        self,
        *,
        response: TransportResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.response = response or TransportResponse(
            status_code=200,
            normalized_response=_response(),
        )
        self.error = error
        self.requests: list[TransportRequest] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = asyncio.Event()

    async def send(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        self.started.set()
        try:
            await self.release.wait()
            if self.error is not None:
                raise self.error
            return self.response
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.finished.set()


class _RecordingSecretResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._resolver = StaticSecretResolver(values)
        self.resolved: list[SecretRef] = []

    def resolve(self, ref: SecretRef) -> ResolvedSecret:
        self.resolved.append(ref)
        return self._resolver.resolve(ref)


class _Token:
    def __init__(self, *, cancelled: bool = False, reason: str | None = None) -> None:
        self._cancelled = cancelled
        self._reason = reason
        self._event = asyncio.Event()
        self.wait_started = asyncio.Event()
        self.wait_finished = asyncio.Event()
        if cancelled:
            self._event.set()

    @property
    def cancellation_id(self) -> str:
        return "cancel-1"

    def is_cancelled(self) -> bool:
        return self._cancelled

    async def wait(self) -> None:
        self.wait_started.set()
        try:
            await self._event.wait()
        finally:
            self.wait_finished.set()

    def cancel(self, reason: str | None = None) -> None:
        self._cancelled = True
        self._reason = reason
        self._event.set()

    @property
    def reason(self) -> str | None:
        return self._reason


class _HandoffToken(_Token):
    def __init__(self) -> None:
        super().__init__()
        self.handoff_started = asyncio.Event()
        self.release_handoff = asyncio.Event()

    async def wait(self) -> None:
        self.wait_started.set()
        try:
            await self._event.wait()
        except asyncio.CancelledError:
            self.handoff_started.set()
            await self.release_handoff.wait()
            raise
        finally:
            self.wait_finished.set()


class _CancellationResolver:
    def __init__(self, token: _Token) -> None:
        self.token = token

    def resolve(self, ref: CancellationRef) -> _Token:
        assert ref.cancellation_id == "cancel-1"
        return self.token


class _Clock:
    def __init__(self, values: list[float]) -> None:
        self.values = values

    def monotonic(self) -> float:
        return self.values.pop(0) if self.values else 0.0


def _client(
    *,
    profile: ResolvedModelProfile | None = None,
    response: TransportResponse | None = None,
    token: _Token | None = None,
    clock: _Clock | None = None,
    secret_resolver: _RecordingSecretResolver | None = None,
    local_timeout_seconds: float | None = None,
) -> tuple[DefaultModelClient, _RecordingTransport]:
    transport = _RecordingTransport(
        response
        or TransportResponse(
            status_code=200,
            provider_request_id="provider-req-1",
            normalized_response=_response(),
        )
    )
    client = DefaultModelClient(
        profile_resolver=StaticModelProfileResolver(
            {(profile or _profile()).profile_id: profile or _profile()}
        ),
        secret_resolver=secret_resolver
        or StaticSecretResolver({"openai-key": "sk-real-secret"}),
        transport=transport,
        cancellation_resolver=_CancellationResolver(token or _Token()),
        clock=clock or _Clock([0.0, 0.0, 0.0]),
        local_timeout_seconds=local_timeout_seconds,
    )
    return client, transport


def _controlled_client(
    transport: _ControlledTransport,
    token: _Token,
    *,
    profile: ResolvedModelProfile | None = None,
    clock: _Clock | None = None,
) -> DefaultModelClient:
    resolved_profile = profile or _profile()
    return DefaultModelClient(
        profile_resolver=StaticModelProfileResolver(
            {resolved_profile.profile_id: resolved_profile}
        ),
        secret_resolver=StaticSecretResolver({"openai-key": "sk-real-secret"}),
        transport=transport,
        cancellation_resolver=_CancellationResolver(token),
        clock=clock or _Clock([0.0, 0.0, 0.0]),
    )


async def _assert_no_model_request_tasks() -> None:
    await asyncio.sleep(0)
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name().startswith("millforge-model-")
    ]


@pytest.mark.asyncio
@pytest.mark.live_model_backend
@pytest.mark.skipif(
    not live_model_backend_smoke_enabled(),
    reason=(
        f"set {LIVE_MODEL_BACKEND_SMOKE_FLAG}=1 and configure "
        f"{', '.join(LIVE_MODEL_BACKEND_ENV_VARS)} to run live model backend smoke"
    ),
)
async def test_live_openai_compatible_model_backend_smoke(
    record_property: Any,
) -> None:
    secret_ref = SecretRef(
        secret_id=_live_env("MILLFORGE_LIVE_MODEL_SECRET_ID"),
        env_var=_live_env("MILLFORGE_LIVE_MODEL_SECRET_ENV_VAR"),
    )
    profile = _live_model_profile(secret_ref)
    started = time.monotonic()
    deadline = Deadline(
        started_monotonic=started,
        outer_deadline_monotonic=started + profile.timeout_seconds,
        effective_deadline_monotonic=started + profile.timeout_seconds,
        source="request",
    )
    request = ModelCompletionRequest(
        request_id="live-model-backend-smoke",
        run_id="live-model-backend-smoke",
        model_profile_id=profile.profile_id,
        messages=(UserMessage(content="Reply with a short health check."),),
        deadline=deadline,
        cancellation=CancellationRef(cancellation_id="live-model-backend-smoke"),
        secret_refs=(secret_ref,),
    )
    transport = OpenAIChatCompletionsTransport(
        timeout_seconds=profile.transport.timeout_seconds
    )
    client = DefaultModelClient(
        profile_resolver=StaticModelProfileResolver({profile.profile_id: profile}),
        secret_resolver=_EnvironmentSecretResolver(),
        transport=transport,
        cancellation_resolver=_CancellationResolver(_Token()),
    )

    try:
        response = await client.complete(request)
    finally:
        await client.aclose()

    latency_ms = int((time.monotonic() - started) * 1000)
    _record_sanitized_smoke_report(
        record_property,
        profile=profile,
        response=response,
        latency_ms=latency_ms,
    )

    assert response.model_id
    assert response.finish_reason in {
        "stop",
        "tool_calls",
        "length",
        "content_filter",
        "cancelled",
        "unknown",
    }


def test_endpoint_normalizes_api_prefix_and_rejects_unsafe_urls() -> None:
    endpoint = EndpointConfig(base_url="api.example.test/v1/")

    assert endpoint.base_url == "https://api.example.test/v1"
    assert endpoint.chat_completions_url == (
        "https://api.example.test/v1/chat/completions"
    )

    with pytest.raises(ValidationError, match="userinfo"):
        EndpointConfig(base_url="https://user:pass@example.test/v1")
    with pytest.raises(ValidationError, match="query"):
        EndpointConfig(base_url="https://example.test/v1?api_key=secret")
    with pytest.raises(ValidationError, match="/chat/completions"):
        EndpointConfig(base_url="https://example.test/v1/chat/completions")
    with pytest.raises(ValidationError, match="local testing"):
        EndpointConfig(base_url="http://api.example.test/v1")

    local = EndpointConfig(
        base_url="http://localhost:8080/v1",
        allow_insecure_local=True,
    )
    assert local.chat_completions_url == "http://localhost:8080/v1/chat/completions"


def test_authentication_policy_rejects_unsafe_headers_and_builds_only_auth_headers() -> (
    None
):
    secret = _secret()
    policy = AuthenticationPolicy(
        scheme=AuthenticationScheme.HEADER,
        secret_ref=secret,
        header_name="X-Api-Key",
        allowed_custom_header_names=("x-api-key",),
    )
    resolved = resolve_authentication_secret(
        policy,
        _request(secret_refs=(secret,)).secret_refs,
        StaticSecretResolver({"openai-key": "sk-secret-value"}),
    )

    assert build_auth_headers(policy, resolved) == {"X-Api-Key": "sk-secret-value"}

    with pytest.raises(ValidationError, match="protected"):
        AuthenticationPolicy(
            scheme=AuthenticationScheme.HEADER,
            secret_ref=secret,
            header_name="Authorization",
            allowed_custom_header_names=("authorization",),
        )
    with pytest.raises(ValidationError, match="allowlisted"):
        AuthenticationPolicy(
            scheme=AuthenticationScheme.HEADER,
            secret_ref=secret,
            header_name="X-Other",
            allowed_custom_header_names=("x-api-key",),
        )
    with pytest.raises(ValidationError, match="protected"):
        HeaderValuePolicy(values={"Content-Type": "application/json"})


def test_resolved_secret_is_not_pydantic_or_json_serializable_and_suppresses_repr() -> (
    None
):
    secret = ResolvedSecret("sk-live-secret")

    assert "sk-live-secret" not in repr(secret)
    assert "sk-live-secret" not in str(secret)
    assert secret.reveal_for_header() == "sk-live-secret"
    assert not hasattr(secret, "model_dump")
    with pytest.raises(TypeError):
        json.dumps({"secret": secret})


def test_secret_resolution_requires_request_admission_before_raw_exposure() -> None:
    policy = AuthenticationPolicy(
        scheme=AuthenticationScheme.BEARER,
        secret_ref=_secret(),
    )

    with pytest.raises(SecretResolutionError, match="not admitted"):
        resolve_authentication_secret(
            policy, (), StaticSecretResolver({"openai-key": "sk"})
        )

    resolved = resolve_authentication_secret(
        policy,
        (_secret(),),
        StaticSecretResolver({"openai-key": "sk-real-secret"}),
    )
    assert isinstance(resolved, ResolvedSecret)
    assert build_auth_headers(policy, resolved) == {
        "Authorization": "Bearer sk-real-secret"
    }

    class LeakyResolver:
        def resolve(self, _ref: SecretRef) -> ResolvedSecret:
            raise RuntimeError("sk-raw-resolver-secret")

    with pytest.raises(SecretResolutionError) as caught:
        resolve_authentication_secret(policy, (_secret(),), LeakyResolver())
    assert "sk-raw-resolver-secret" not in repr(caught.value)


def test_static_profile_resolver_exact_lookup_and_sanitized_diagnostics() -> None:
    profile = _profile()
    resolver = StaticModelProfileResolver({"profile.openai": profile})

    assert resolver.resolve("profile.openai") is profile
    assert resolver.diagnostics("profile.openai") == {
        "source_name": "unit-test",
        "source_digest": "digest:abc123",
    }
    with pytest.raises(ModelBackendConfigError, match="unknown model profile"):
        resolver.resolve("missing")
    with pytest.raises(ModelBackendConfigError, match="mapping key"):
        StaticModelProfileResolver({"wrong": profile})


def test_capability_negotiator_treats_required_unknown_as_unsupported() -> None:
    negotiator = CapabilityNegotiator()
    negotiator.negotiate(
        ModelCapabilityRequirements(),
        _profile().capabilities,
    )

    with pytest.raises(ModelProviderError) as exc_info:
        negotiator.negotiate(
            ModelCapabilityRequirements(),
            CapabilityDeclarations(
                support={
                    "tool_calls": CapabilitySupport.UNKNOWN,
                    "system_messages": CapabilitySupport.SUPPORTED,
                    "tool_result_messages": CapabilitySupport.SUPPORTED,
                }
            ),
        )

    error = exc_info.value
    assert error.category is ProviderErrorCategory.UNSUPPORTED_CAPABILITY
    assert error.retryable is False
    assert dict(error.fields) == {"tool_calls": "unknown"}


def test_sampling_and_request_option_allowlists_reject_unknown_or_protected_fields() -> (
    None
):
    assert "temperature" in SamplingPolicy().allowed_overrides
    with pytest.raises(ValidationError, match="unknown sampling override"):
        SamplingPolicy(allowed_overrides=("model",))
    with pytest.raises(ValidationError, match="protected"):
        RequestOptionAllowlist(allowed_options=("headers",))


def test_transport_safety_defaults_reject_redirects_and_environment_proxy_trust() -> (
    None
):
    transport = TransportConfig()
    assert transport.follow_redirects is False
    assert transport.trust_env is False

    with pytest.raises(ValidationError, match="redirects"):
        TransportConfig(follow_redirects=True)
    with pytest.raises(ValidationError, match="environment proxies"):
        TransportConfig(trust_env=True)
    with pytest.raises(ValidationError, match="finite"):
        TransportConfig(timeout_seconds=float("inf"))


def test_explicit_live_timeouts_are_positive_finite_typed_contracts() -> None:
    timeouts = OpenAICompatibleTimeouts(
        connect_seconds=1,
        read_seconds=2,
        write_seconds=3,
        pool_seconds=4,
        local_total_seconds=5,
    )

    assert timeouts.read_seconds == 2
    for value in (0, -1, float("inf"), float("nan"), True):
        with pytest.raises(ModelBackendConfigError, match="positive finite"):
            OpenAICompatibleTimeouts(
                connect_seconds=value,
                read_seconds=2,
                write_seconds=3,
                pool_seconds=4,
                local_total_seconds=5,
            )


def test_resolved_profile_exposes_canonical_immutable_profile_contract() -> None:
    profile = _profile()

    assert profile.model_dump(mode="json")["transport_id"] == "openai-chat-completions"
    assert profile.timeout_seconds == 60.0
    assert profile.maximum_output_tokens == 4096
    assert profile.sampling.model_dump(mode="json") == {
        "temperature": None,
        "top_p": None,
        "presence_penalty": None,
        "frequency_penalty": None,
        "seed": None,
        "stop": None,
        "allowed_overrides": [
            "temperature",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "seed",
            "stop",
        ],
        "allow_maximum_output_tokens_override": True,
    }
    with pytest.raises(ValidationError, match="frozen"):
        profile.model_id = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="extra"):
        ResolvedModelProfile(
            profile_id="profile.bad",
            provider_id="compat",
            model_id="model",
            endpoint=EndpointConfig(base_url="https://api.example.test/v1"),
            authentication=AuthenticationPolicy(scheme=AuthenticationScheme.NONE),
            source_digest="digest:bad",
            api_key="sk-secret",  # type: ignore[call-arg]
        )


def test_redaction_covers_urls_tokens_error_fields_and_diagnostic_mappings() -> None:
    raw_secret = "sk-secret-value"

    assert DEFAULT_REDACTION_POLICY.max_string_length == 2048
    assert raw_secret not in redact_text(
        f"Authorization: Bearer {raw_secret}",
        secret_values=(raw_secret,),
    )
    assert redact_text("https://user:pass@example.test/v1?api_key=abc") == (
        "https://example.test/v1?api_key=**redacted**"
    )
    assert (
        redact_text("failed with https://user:pass@example.test/v1?api_key=abc")
        == "failed with https://example.test/v1?api_key=**redacted**"
    )
    assert redact_text("failed with :// and api_key=abc") == (
        "failed with :// and api_key**redacted**"
    )
    assert sanitize_provider_error_fields(
        {"Authorization": "Bearer sk-secret-value", "message": "bad token=abc"},
        secret_values=(raw_secret,),
    ) == {
        "Authorization": "**redacted**",
        "message": "bad token**redacted**",
    }
    assert sanitize_provider_error_fields(
        {"message": "provider failed at https://user:pass@example.test/v1?token=abc"}
    ) == {"message": ("provider failed at https://example.test/v1?token=**redacted**")}
    assert redact_mapping(
        {
            "trace": f"provider failed with {raw_secret}",
            "api_key": raw_secret,
            "count": 1,
        },
        secret_values=(raw_secret,),
    ) == {
        "trace": "provider failed with **redacted**",
        "api_key": "**redacted**",
        "count": 1,
    }


def test_provider_error_repr_and_message_are_sanitized_and_bounded() -> None:
    error = ModelProviderError(
        category=ProviderErrorCategory.AUTHENTICATION,
        message="Bearer sk-secret-value was rejected",
        fields={"raw_token": "sk-secret-value", "safe": "ok"},
    )

    rendered = f"{error!r} {error}"
    assert "sk-secret-value" not in rendered
    assert error.retryable is False
    assert dict(error.fields)["raw_token"] == "**redacted**"


def test_live_composition_contracts_are_exported_without_private_clients() -> None:
    import millforge

    assert "ResolvedModelProfile" in millforge.__all__
    assert "AuthenticationPolicy" in millforge.__all__
    assert "OpenAICompatibleTimeouts" in millforge.__all__
    assert "DefaultModelClient" not in millforge.__all__
    assert "OpenAIChatCompletionsTransport" not in millforge.__all__


def test_model_client_protocol_signature_has_no_backend_transport_fields() -> None:
    from millforge.protocols import ModelClient

    signature = inspect.signature(ModelClient.complete)
    assert list(signature.parameters) == ["self", "request"]
    assert "endpoint" not in str(signature)
    assert "authentication" not in str(signature)
    assert "http" not in str(signature).lower()


@pytest.mark.asyncio
async def test_default_model_client_orchestrates_single_transport_request() -> None:
    secret = _secret()
    client, transport = _client()

    original = _request(secret_refs=(secret,))
    response = await client.complete(original)

    assert response.content == "done"
    assert response.provider_request_id == "provider-req-1"
    assert len(transport.requests) == 1
    sent = transport.requests[0]
    assert sent.public_request is original
    assert sent.profile.profile_id == original.model_profile_id
    assert sent.url == "https://api.example.test/v1/chat/completions"
    assert list(sent.headers) == [
        "Content-Type",
        "User-Agent",
        "Authorization",
    ]
    assert sent.headers["Authorization"] == "Bearer sk-real-secret"
    assert sent.body["model"] == "gpt-test"
    assert sent.body["stream"] is False
    assert original.model_dump() == _request(secret_refs=(secret,)).model_dump()


@pytest.mark.asyncio
async def test_default_model_client_effective_timeout_uses_profile_deadline_minimum() -> (
    None
):
    profile = _profile().model_copy(update={"timeout_seconds": 10.0})
    client, transport = _client(profile=profile, clock=_Clock([57.0, 57.0]))

    await client.complete(_request(secret_refs=(_secret(),)))

    assert transport.requests[0].timeout_seconds == 3.0

    local_client, local_transport = _client(
        profile=profile,
        clock=_Clock([1.0, 1.0]),
        local_timeout_seconds=2.0,
    )
    await local_client.complete(_request(secret_refs=(_secret(),)))
    assert local_transport.requests[0].timeout_seconds == 2.0


@pytest.mark.asyncio
async def test_default_model_client_executes_canonical_builder_profiles_through_http_transport() -> (
    None
):
    seen: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": body,
            }
        )
        if request.url.host == "compat-a.test":
            assert request.headers["authorization"] == "Bearer sk-compat-a-secret"
            assert "accept" not in request.headers
            assert body == {
                "model": "fake-tools-a",
                "messages": [{"role": "user", "content": "profile a ping"}],
                "stream": False,
                "max_tokens": 4096,
            }
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "x-request-id": "provider-openai-1",
                },
                json={
                    "model": "fake-tools-a",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "profile a ok",
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 5,
                        "total_tokens": 9,
                    },
                },
            )

        assert request.url.host == "compat-b.test"
        assert request.headers["x-api-key"] == "sk-compat-b-secret"
        assert "accept" not in request.headers
        assert body == {
            "model": "fake-tools-b",
            "messages": [{"role": "user", "content": "profile b ping"}],
            "stream": False,
            "max_tokens": 4096,
            "reasoning_effort": "high",
        }
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "provider-compat-b-1",
                "model": "fake-tools-b",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "profile b ok",
                        },
                    }
                ],
            },
        )

    profile_a = make_canonical_builder_profile_a()
    profile_b = make_canonical_builder_profile_b()
    assert profile_a.profile_id == BUILDER_FIXTURE_PROFILE_ID
    assert profile_a.provider_id == "compat-a"
    assert profile_a.model_id == "fake-tools-a"
    assert profile_a.transport_id == "openai.chat_completions.v1"
    assert profile_a.endpoint.base_url == "https://compat-a.test/v1"
    assert profile_a.authentication.secret_ref == BUILDER_COMPAT_A_SECRET
    assert profile_a.authentication.scheme is AuthenticationScheme.BEARER
    assert profile_a.capabilities.state_for("usage_reporting") is (
        CapabilitySupport.SUPPORTED
    )
    assert profile_a.capabilities.state_for("tool_calls") is CapabilitySupport.SUPPORTED
    assert profile_a.capabilities.state_for("parallel_tool_calls") is (
        CapabilitySupport.UNSUPPORTED
    )
    assert profile_a.reasoning.mode is ReasoningMode.DISABLED
    assert profile_a.sampling.allowed_overrides == ()
    assert profile_a.sampling.allow_maximum_output_tokens_override is False

    assert profile_b.profile_id == BUILDER_FIXTURE_PROFILE_ID
    assert profile_b.provider_id == "compat-b"
    assert profile_b.model_id == "fake-tools-b"
    assert profile_b.transport_id == "openai.chat_completions.v1"
    assert profile_b.endpoint.base_url == "https://compat-b.test/openai/v1"
    assert profile_b.authentication.secret_ref == BUILDER_COMPAT_B_SECRET
    assert profile_b.authentication.scheme is AuthenticationScheme.HEADER
    assert profile_b.authentication.header_name == "X-API-Key"
    assert profile_b.capabilities.state_for("usage_reporting") is (
        CapabilitySupport.UNSUPPORTED
    )
    assert profile_b.capabilities.state_for("tool_calls") is CapabilitySupport.SUPPORTED
    assert profile_b.capabilities.state_for("parallel_tool_calls") is (
        CapabilitySupport.UNSUPPORTED
    )
    assert profile_b.reasoning.mode is ReasoningMode.ENABLED
    assert profile_b.reasoning.effort is ReasoningEffort.HIGH
    assert profile_b.reasoning.effort_field == "reasoning_effort"
    assert profile_b.error_mappings.request_id_paths == ("error.request_id", "id")
    assert profile_b.sampling.allowed_overrides == ()
    assert profile_b.sampling.allow_maximum_output_tokens_override is False

    async def complete(
        profile: ResolvedModelProfile, secret: SecretRef
    ) -> ModelCompletionResponse:
        transport = OpenAIChatCompletionsTransport(
            http_transport=httpx.MockTransport(handler)
        )
        client = DefaultModelClient(
            profile_resolver=StaticModelProfileResolver({profile.profile_id: profile}),
            secret_resolver=StaticSecretResolver(
                {
                    "compat_a_key": "sk-compat-a-secret",
                    "compat_b_key": "sk-compat-b-secret",
                }
            ),
            transport=transport,
            cancellation_resolver=_CancellationResolver(_Token()),
            clock=_Clock([0.0, 0.0, 0.0]),
        )
        response = await client.complete(
            _request(secret_refs=(secret,)).model_copy(
                update={
                    "request_id": f"req-{profile.provider_id}",
                    "model_profile_id": profile.profile_id,
                    "messages": (
                        UserMessage(content=f"profile {profile.provider_id[-1]} ping"),
                    ),
                }
            )
        )
        await client.aclose()
        return response

    profile_a_response = await complete(profile_a, BUILDER_COMPAT_A_SECRET)
    profile_b_response = await complete(profile_b, BUILDER_COMPAT_B_SECRET)

    assert profile_a_response == ModelCompletionResponse(
        provider_request_id="provider-openai-1",
        model_id="fake-tools-a",
        message=AssistantMessage(content="profile a ok"),
        finish_reason="stop",
        usage=TokenUsage(
            input_tokens=4,
            output_tokens=5,
            total_tokens=9,
            provider_reported=True,
        ),
    )
    assert profile_b_response == ModelCompletionResponse(
        provider_request_id="provider-compat-b-1",
        model_id="fake-tools-b",
        message=AssistantMessage(content="profile b ok"),
        finish_reason="stop",
        usage=None,
    )
    assert [item["url"] for item in seen] == [
        "https://compat-a.test/v1/chat/completions",
        "https://compat-b.test/openai/v1/chat/completions",
    ]


@pytest.mark.asyncio
async def test_default_model_client_rejects_unknown_profile_before_transport() -> None:
    client, transport = _client()

    with pytest.raises(ModelBackendConfigError, match="unknown model profile"):
        await client.complete(
            _request(secret_refs=(_secret(),)).model_copy(
                update={"model_profile_id": "missing"}
            )
        )

    assert transport.requests == []


@pytest.mark.asyncio
async def test_default_model_client_rejects_required_unknown_capability_before_transport() -> (
    None
):
    profile = _profile().model_copy(
        update={
            "capabilities": CapabilityDeclarations(
                support={
                    "tool_calls": CapabilitySupport.UNKNOWN,
                    "system_messages": CapabilitySupport.SUPPORTED,
                    "tool_result_messages": CapabilitySupport.SUPPORTED,
                }
            )
        }
    )
    secret_resolver = _RecordingSecretResolver({"openai-key": "sk-real-secret"})
    client, transport = _client(profile=profile, secret_resolver=secret_resolver)

    with pytest.raises(ModelProviderError) as exc_info:
        await client.complete(_request(secret_refs=(_secret(),)))

    assert exc_info.value.category is ProviderErrorCategory.UNSUPPORTED_CAPABILITY
    assert secret_resolver.resolved == []
    assert transport.requests == []


@pytest.mark.asyncio
async def test_default_model_client_rejects_disallowed_sampling_and_token_overrides() -> (
    None
):
    profile = _profile().model_copy(
        update={
            "sampling": SamplingPolicy(
                allowed_overrides=("temperature",),
                allow_maximum_output_tokens_override=False,
            )
        }
    )
    client, transport = _client(profile=profile)
    request = _request(secret_refs=(_secret(),)).model_copy(
        update={"sampling_overrides": SamplingRequest(top_p=0.5)}
    )

    with pytest.raises(ModelBackendConfigError, match="top_p"):
        await client.complete(request)
    with pytest.raises(ModelBackendConfigError, match="maximum output token"):
        await client.complete(
            _request(secret_refs=(_secret(),)).model_copy(
                update={"maximum_output_tokens_override": 10}
            )
        )
    assert transport.requests == []


@pytest.mark.asyncio
async def test_default_model_client_rejects_required_reasoning_without_mapping_before_secret_resolution() -> (
    None
):
    profile = _profile().model_copy(
        update={"reasoning": ReasoningPolicy(mode=ReasoningMode.REQUIRED)}
    )
    client, transport = _client(profile=profile)

    with pytest.raises(ModelBackendConfigError, match="required reasoning"):
        await client.complete(_request(secret_refs=(_secret(),)))

    assert transport.requests == []


@pytest.mark.asyncio
async def test_default_model_client_applies_only_allowed_request_options() -> None:
    profile = _profile().model_copy(
        update={
            "request_options": RequestOptionAllowlist(
                allowed_options=(
                    "tool_choice",
                    "parallel_tool_calls",
                    "response_format",
                    "user",
                )
            )
        }
    )
    client, transport = _client(profile=profile)

    await client.complete(
        _request(secret_refs=(_secret(),)).model_copy(
            update={
                "request_options": {
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                    "response_format": {"type": "json_object"},
                    "user": "run-user",
                }
            }
        )
    )

    body = transport.requests[0].body
    assert body["tool_choice"] == "auto"
    assert body["parallel_tool_calls"] is False
    assert body["response_format"] == {"type": "json_object"}
    assert body["user"] == "run-user"

    secret_resolver = _RecordingSecretResolver({"openai-key": "sk-real-secret"})
    default_client, default_transport = _client(secret_resolver=secret_resolver)
    with pytest.raises(ModelBackendConfigError, match="not allowed"):
        await default_client.complete(
            _request(secret_refs=(_secret(),)).model_copy(
                update={"request_options": {"tool_choice": "required"}}
            )
        )
    with pytest.raises(ModelBackendConfigError, match="not allowed"):
        await default_client.complete(
            _request(secret_refs=(_secret(),)).model_copy(
                update={"request_options": {"headers": {"Authorization": "bad"}}}
            )
        )
    assert secret_resolver.resolved == []
    assert default_transport.requests == []


@pytest.mark.asyncio
async def test_default_model_client_merges_allowed_sampling_and_output_token_policy() -> (
    None
):
    profile = _profile().model_copy(
        update={
            "maximum_output_tokens": 20,
            "sampling": SamplingPolicy(
                temperature=0.2,
            ),
        }
    )
    client, transport = _client(profile=profile)

    await client.complete(
        _request(secret_refs=(_secret(),)).model_copy(
            update={"sampling_overrides": SamplingRequest(temperature=0.7)}
        )
    )

    body = transport.requests[0].body
    assert body["temperature"] == 0.7
    assert body["max_tokens"] == 20


@pytest.mark.asyncio
async def test_default_model_client_maps_reasoning_without_owned_field_leakage() -> (
    None
):
    profile = _profile().model_copy(
        update={
            "sampling": SamplingPolicy(
                temperature=0.2,
            ),
            "reasoning": ReasoningPolicy(
                mode=ReasoningMode.ENABLED,
                effort=ReasoningEffort.MEDIUM,
                mode_field="reasoning",
                effort_field="reasoning_effort_level",
                mode_values={ReasoningMode.ENABLED: "auto"},
                effort_values={ReasoningEffort.MEDIUM: "medium"},
            ),
        }
    )
    client, transport = _client(profile=profile)

    await client.complete(_request(secret_refs=(_secret(),)))

    body = transport.requests[0].body
    assert body["temperature"] == 0.2
    assert body["reasoning"] == "auto"
    assert body["reasoning_effort_level"] == "medium"
    assert "reasoning_mode" not in body
    assert "reasoning_effort" not in body


@pytest.mark.asyncio
async def test_reasoning_policy_replay_field_requires_enabled_or_required_mapped_mode() -> (
    None
):
    for mode in (ReasoningMode.ENABLED, ReasoningMode.REQUIRED):
        for mode_field, mode_values in (
            (None, {mode: "on"}),
            ("   ", {mode: "on"}),
            ("thinking", {}),
        ):
            with pytest.raises(ValidationError, match="replay|mapping|wire"):
                ReasoningPolicy(
                    mode=mode,
                    mode_field=mode_field,
                    mode_values=mode_values,
                    tool_call_replay_field="reasoning_content",
                )
        assert (
            ReasoningPolicy(
                mode=mode,
                mode_field="thinking",
                mode_values={mode: "on"},
                tool_call_replay_field="reasoning_content",
            ).tool_call_replay_field
            == "reasoning_content"
        )

    with pytest.raises(ValidationError, match="replay|enabled|required"):
        ReasoningPolicy(
            mode=ReasoningMode.DISABLED,
            tool_call_replay_field="reasoning_content",
        )
    with pytest.raises(ValidationError, match="literal|reasoning_content"):
        ReasoningPolicy(
            mode=ReasoningMode.ENABLED,
            mode_field="thinking",
            mode_values={ReasoningMode.ENABLED: "on"},
            tool_call_replay_field="provider_state",  # type: ignore[arg-type]
        )

    invalid_runtime_policy = ReasoningPolicy.model_construct(
        mode=ReasoningMode.REQUIRED,
        effort=None,
        mode_field=None,
        effort_field=None,
        mode_values={},
        effort_values={},
        tool_call_replay_field="reasoning_content",
    )
    profile = _profile().model_copy(update={"reasoning": invalid_runtime_policy})
    secret_resolver = _RecordingSecretResolver({"openai-key": "sk-real-secret"})
    client, transport = _client(
        profile=profile,
        secret_resolver=secret_resolver,
    )

    with pytest.raises(ModelBackendConfigError, match="reasoning|replay|mapping"):
        await client.complete(_request(secret_refs=(_secret(),)))

    assert secret_resolver.resolved == []
    assert transport.requests == []


@pytest.mark.asyncio
async def test_reasoning_tool_response_preserves_content_calls_and_continuation() -> (
    None
):
    continuation = "  exact λ continuation\n"
    profile = _replay_profile(provider_id="opaque-provider-replay")
    client, transport = _client(
        profile=profile,
        response=_raw_reasoning_response(continuation),
    )

    response = await client.complete(_request(secret_refs=(_secret(),)))

    assert transport.requests[0].profile.provider_id == "opaque-provider-replay"
    assert response.content == "ordinary assistant content"
    assert response.tool_calls == (_reasoning_tool_call(),)
    assert response.message.reasoning_content == continuation
    assert (
        AssistantMessage.model_validate(
            response.message.model_dump(mode="json")
        ).reasoning_content
        == continuation
    )
    assert "reasoning_content" not in AssistantMessage(content="legacy").model_dump(
        mode="json"
    )
    assert continuation not in repr(response.message)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "include"),
    (
        (None, False),
        (None, True),
        ("", True),
        (" \n\t", True),
        (42, True),
        ("12345", True),
    ),
)
async def test_required_reasoning_continuation_refuses_malformed_provider_response(
    value: object,
    include: bool,
) -> None:
    profile = _replay_profile(success_body_limit_bytes=4)
    client, _ = _client(
        profile=profile,
        response=_raw_reasoning_response(
            value,
            include_reasoning_content=include,
        ),
    )

    with pytest.raises(ModelProviderError) as exc_info:
        await client.complete(_request(secret_refs=(_secret(),)))

    assert exc_info.value.category is ProviderErrorCategory.MALFORMED_RESPONSE
    assert str(value) not in str(exc_info.value) or value in (None, "")


@pytest.mark.asyncio
async def test_raw_and_normalized_responses_share_reasoning_replay_admission_matrix() -> (
    None
):
    malformed_values: tuple[tuple[object, bool], ...] = (
        (None, False),
        (None, True),
        ("", True),
        ("   ", True),
        (7, True),
        ("12345", True),
    )
    profile = _replay_profile(success_body_limit_bytes=4)

    for value, include in malformed_values:
        raw_client, _ = _client(
            profile=profile,
            response=_raw_reasoning_response(
                value,
                include_reasoning_content=include,
            ),
        )
        normalized = _normalized_reasoning_response(value)
        if not include:
            object.__delattr__(normalized.message, "reasoning_content")
        normalized_client, _ = _client(
            profile=profile,
            response=TransportResponse(
                status_code=200,
                normalized_response=normalized,
            ),
        )
        for client in (raw_client, normalized_client):
            with pytest.raises(ModelProviderError) as exc_info:
                await client.complete(_request(secret_refs=(_secret(),)))
            assert exc_info.value.category is ProviderErrorCategory.MALFORMED_RESPONSE

    for transport_response in (
        _raw_reasoning_response("discard me"),
        TransportResponse(
            status_code=200,
            normalized_response=_normalized_reasoning_response("discard me"),
        ),
    ):
        client, _ = _client(response=transport_response)
        response = await client.complete(_request(secret_refs=(_secret(),)))
        assert response.message.reasoning_content is None
        assert "reasoning_content" not in response.message.model_dump(mode="json")


@pytest.mark.asyncio
async def test_replay_preflight_refuses_incomplete_or_reordered_call_result_history() -> (
    None
):
    first_call = _reasoning_tool_call("provider-call-1")
    second_call = _reasoning_tool_call("provider-call-2").model_copy(
        update={"name": "forecast"}
    )
    replay_turn = AssistantMessage(
        content="ordinary",
        reasoning_content="private continuation",
        tool_calls=(first_call, second_call),
    )
    first_result = ToolResultMessage(
        tool_call_id=first_call.call_id,
        tool_name=first_call.name,
        content="first",
    )
    second_result = ToolResultMessage(
        tool_call_id=second_call.call_id,
        tool_name=second_call.name,
        content="second",
    )
    corrupt_histories = (
        (
            AssistantMessage.model_construct(
                role="assistant",
                content="duplicate provider call IDs",
                reasoning_content="private continuation",
                tool_calls=(first_call, first_call),
            ),
            first_result,
            first_result,
        ),
        (replay_turn, first_result),
        (replay_turn, first_result, first_result, second_result),
        (replay_turn, second_result, first_result),
        (
            replay_turn,
            first_result,
            ToolResultMessage.model_construct(
                role="tool",
                tool_call_id=second_call.call_id,
                tool_name="wrong-name",
                content="second",
            ),
        ),
        (
            replay_turn,
            first_result,
            AssistantMessage(content="too early"),
            second_result,
        ),
        (
            ToolResultMessage.model_construct(
                role="tool",
                tool_call_id="orphan",
                tool_name="lookup",
                content="orphan",
            ),
        ),
        (
            AssistantMessage(
                content="missing continuation",
                tool_calls=(first_call,),
            ),
            first_result,
        ),
    )

    for messages in corrupt_histories:
        secret_resolver = _RecordingSecretResolver({"openai-key": "sk-real-secret"})
        client, transport = _client(
            profile=_replay_profile(),
            secret_resolver=secret_resolver,
        )
        request = _request(secret_refs=(_secret(),)).model_copy(
            update={"messages": messages}
        )
        with pytest.raises(ModelBackendConfigError, match="replay|tool|result"):
            await client.complete(request)
        assert secret_resolver.resolved == []
        assert transport.requests == []

    secret_resolver = _RecordingSecretResolver({"openai-key": "sk-real-secret"})
    client, transport = _client(secret_resolver=secret_resolver)
    unselected_request = _request(secret_refs=(_secret(),)).model_copy(
        update={
            "messages": (
                AssistantMessage(
                    content="ordinary",
                    reasoning_content="must not pass through",
                    tool_calls=(first_call,),
                ),
                first_result,
            )
        }
    )
    with pytest.raises(ModelBackendConfigError, match="reasoning|replay"):
        await client.complete(unselected_request)
    assert secret_resolver.resolved == []
    assert transport.requests == []


@pytest.mark.asyncio
async def test_default_model_client_secret_mismatch_missing_duplicate_and_unknown_refs() -> (
    None
):
    client, transport = _client()

    with pytest.raises(SecretResolutionError, match="exactly one"):
        await client.complete(_request())
    with pytest.raises(SecretResolutionError, match="does not match"):
        await client.complete(
            _request(
                secret_refs=(
                    SecretRef(secret_id="openai-key", env_var="OTHER_API_KEY"),
                )
            )
        )
    duplicated = _request(secret_refs=(_secret(),)).model_copy(
        update={"secret_refs": (_secret(), _secret())}
    )
    with pytest.raises(SecretResolutionError, match="duplicate"):
        await client.complete(duplicated)

    no_auth_profile = _profile().model_copy(
        update={
            "authentication": AuthenticationPolicy(scheme=AuthenticationScheme.NONE)
        }
    )
    no_auth_client, _ = _client(profile=no_auth_profile)
    with pytest.raises(SecretResolutionError, match="unknown"):
        await no_auth_client.complete(_request(secret_refs=(_secret(),)))
    assert transport.requests == []


@pytest.mark.asyncio
async def test_default_model_client_checks_cancellation_and_deadline_around_transport() -> (
    None
):
    cancelled_client, cancelled_transport = _client(
        token=_Token(cancelled=True, reason="Bearer sk-leaked")
    )
    with pytest.raises(ModelProviderError) as exc_info:
        await cancelled_client.complete(_request(secret_refs=(_secret(),)))
    assert exc_info.value.category is ProviderErrorCategory.CANCELLED
    assert exc_info.value.retryable is False
    assert "sk-leaked" not in str(exc_info.value)
    assert cancelled_transport.requests == []

    expired_client, expired_transport = _client(clock=_Clock([61.0]))
    with pytest.raises(ModelProviderError, match="deadline expired") as timeout_error:
        await expired_client.complete(_request(secret_refs=(_secret(),)))
    assert timeout_error.value.category is ProviderErrorCategory.TIMEOUT
    assert timeout_error.value.retryable is False
    assert expired_transport.requests == []

    post_transport_client, post_transport = _client(clock=_Clock([0.0, 61.0]))
    with pytest.raises(ModelProviderError, match="deadline expired") as post_error:
        await post_transport_client.complete(_request(secret_refs=(_secret(),)))
    assert post_error.value.category is ProviderErrorCategory.TIMEOUT
    assert post_error.value.retryable is False
    assert len(post_transport.requests) == 1


@pytest.mark.asyncio
async def test_default_model_client_cancels_blocked_transport_when_token_wins() -> None:
    token = _Token()
    transport = _ControlledTransport()
    client = _controlled_client(transport, token)
    completion = asyncio.create_task(
        client.complete(_request(secret_refs=(_secret(),)))
    )
    await transport.started.wait()
    await token.wait_started.wait()

    token.cancel("Authorization: Bearer sk-in-flight-secret")

    with pytest.raises(ModelProviderError) as exc_info:
        await completion
    assert exc_info.value.category is ProviderErrorCategory.CANCELLED
    assert exc_info.value.retryable is False
    assert "sk-in-flight-secret" not in str(exc_info.value)
    assert transport.cancelled.is_set()
    assert transport.finished.is_set()
    assert token.wait_finished.is_set()
    await _assert_no_model_request_tasks()


@pytest.mark.asyncio
async def test_default_model_client_cancels_blocked_transport_at_deadline() -> None:
    token = _Token()
    transport = _ControlledTransport()
    profile = _profile().model_copy(update={"timeout_seconds": 0.01})
    client = _controlled_client(transport, token, profile=profile)

    with pytest.raises(ModelProviderError, match="deadline expired") as exc_info:
        await client.complete(_request(secret_refs=(_secret(),)))

    assert exc_info.value.category is ProviderErrorCategory.TIMEOUT
    assert isinstance(exc_info.value, ModelRequestDeadlineExceededError)
    assert exc_info.value.retryable is False
    assert transport.started.is_set()
    assert transport.cancelled.is_set()
    assert transport.finished.is_set()
    assert token.wait_finished.is_set()
    await _assert_no_model_request_tasks()


@pytest.mark.asyncio
async def test_default_model_client_prefers_cancellation_during_success_handoff() -> (
    None
):
    token = _HandoffToken()
    transport = _ControlledTransport()
    client = _controlled_client(transport, token)
    completion = asyncio.create_task(
        client.complete(_request(secret_refs=(_secret(),)))
    )
    await transport.started.wait()
    await token.wait_started.wait()
    transport.release.set()
    await token.handoff_started.wait()

    assert transport.finished.is_set()
    assert not completion.done()
    token.cancel("cancel during success handoff")
    token.release_handoff.set()

    with pytest.raises(ModelProviderError) as exc_info:
        await completion
    assert exc_info.value.category is ProviderErrorCategory.CANCELLED
    assert not transport.cancelled.is_set()
    assert token.wait_finished.is_set()
    await _assert_no_model_request_tasks()


@pytest.mark.asyncio
async def test_default_model_client_prefers_token_cancel_over_ready_transport() -> None:
    token = _Token()
    transport = _ControlledTransport()
    client = _controlled_client(transport, token)
    completion = asyncio.create_task(
        client.complete(_request(secret_refs=(_secret(),)))
    )
    await transport.started.wait()
    await token.wait_started.wait()

    token.cancel("cancel wins")
    transport.release.set()

    with pytest.raises(ModelProviderError) as exc_info:
        await completion
    assert exc_info.value.category is ProviderErrorCategory.CANCELLED
    assert transport.finished.is_set()
    assert token.wait_finished.is_set()
    await _assert_no_model_request_tasks()


@pytest.mark.asyncio
async def test_default_model_client_prefers_cancellation_during_failure_handoff() -> (
    None
):
    provider_error = ModelProviderError(
        category=ProviderErrorCategory.SERVER_ERROR,
        message="provider failed",
    )
    token = _HandoffToken()
    transport = _ControlledTransport(error=provider_error)
    client = _controlled_client(transport, token)
    completion = asyncio.create_task(
        client.complete(_request(secret_refs=(_secret(),)))
    )
    await transport.started.wait()
    await token.wait_started.wait()
    transport.release.set()
    await token.handoff_started.wait()

    assert transport.finished.is_set()
    assert not completion.done()
    token.cancel("cancel during failure handoff")
    token.release_handoff.set()

    with pytest.raises(ModelProviderError) as exc_info:
        await completion

    assert exc_info.value is not provider_error
    assert exc_info.value.category is ProviderErrorCategory.CANCELLED
    assert token.wait_finished.is_set()
    await _assert_no_model_request_tasks()


@pytest.mark.asyncio
async def test_default_model_client_joins_tasks_when_caller_cancels() -> None:
    token = _Token()
    transport = _ControlledTransport()
    client = _controlled_client(transport, token)
    completion = asyncio.create_task(
        client.complete(_request(secret_refs=(_secret(),)))
    )
    await transport.started.wait()
    await token.wait_started.wait()

    completion.cancel()

    with pytest.raises(asyncio.CancelledError):
        await completion
    assert transport.cancelled.is_set()
    assert transport.finished.is_set()
    assert token.wait_finished.is_set()
    await _assert_no_model_request_tasks()


@pytest.mark.asyncio
async def test_default_model_client_lifecycle_close_is_idempotent_and_context_managed() -> (
    None
):
    client, transport = _client()

    await client.aclose()
    await client.aclose()
    assert transport.close_count == 1

    client_2, transport_2 = _client()
    async with client_2 as entered:
        assert entered is client_2
    assert transport_2.close_count == 1


@pytest.mark.asyncio
async def test_default_model_client_normalizes_raw_provider_response_and_usage() -> (
    None
):
    client, _ = _client(
        response=TransportResponse(
            status_code=200,
            provider_request_id="provider-req-2",
            body={
                "model": "gpt-test",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "hello"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 3,
                    "total_tokens": 5,
                },
            },
        )
    )

    response = await client.complete(_request(secret_refs=(_secret(),)))

    assert response == ModelCompletionResponse(
        provider_request_id="provider-req-2",
        model_id="gpt-test",
        message=AssistantMessage(content="hello"),
        finish_reason="stop",
        usage=TokenUsage(
            input_tokens=2,
            output_tokens=3,
            total_tokens=5,
            provider_reported=True,
        ),
    )


@pytest.mark.asyncio
async def test_default_model_client_preserves_invalid_tool_arguments() -> None:
    client, _ = _client(
        response=TransportResponse(
            status_code=200,
            body={
                "model": "gpt-test",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": "{not-json",
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )
    )

    response = await client.complete(_request(secret_refs=(_secret(),)))

    arguments = response.tool_calls[0].arguments
    assert isinstance(arguments, InvalidToolArguments)
    assert arguments.raw == "{not-json"
    assert arguments.error_code == "malformed_json"


@pytest.mark.asyncio
async def test_default_model_client_rejects_overflowed_tool_argument_number() -> None:
    client, _ = _client(
        response=TransportResponse(
            status_code=200,
            body={
                "model": "gpt-test",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"candidate":1e999}',
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )
    )

    response = await client.complete(_request(secret_refs=(_secret(),)))

    arguments = response.tool_calls[0].arguments
    assert isinstance(arguments, InvalidToolArguments)
    assert arguments.raw == '{"candidate":1e999}'
    assert arguments.error_code == "non_finite_json"


@pytest.mark.asyncio
async def test_default_model_client_rejects_malformed_provider_response() -> None:
    client, _ = _client(
        response=TransportResponse(
            status_code=200,
            body={
                "model": "gpt-test",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "hello"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 3,
                    "total_tokens": 4,
                },
            },
        )
    )

    with pytest.raises(ModelProviderError) as exc_info:
        await client.complete(_request(secret_refs=(_secret(),)))

    assert exc_info.value.category is ProviderErrorCategory.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_openai_chat_transport_posts_exact_serialized_request() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["header_items"] = list(request.headers.multi_items())
        seen["timeout"] = request.extensions["timeout"]
        seen["content_length"] = len(request.content)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "x-request-id": "provider-req-1",
            },
            json={
                "model": "gpt-test",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call-2",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"city":"Hilo"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    profile = _profile()
    expected_body = {
        "model": "gpt-test",
        "stream": False,
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "weather"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"city":"Hilo"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "lookup",
                "content": "sunny",
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Lookup weather",
                    "parameters": {"type": "object"},
                },
            }
        ],
    }
    request = TransportRequest(
        request_id="req-1",
        profile=profile,
        public_request=_request(secret_refs=(_secret(),)),
        url=profile.endpoint.chat_completions_url,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "millforge-model-backend/1",
            "Authorization": "Bearer sk-real-secret",
        },
        body=expected_body,
        timeout_seconds=5,
    )
    transport = OpenAIChatCompletionsTransport(
        http_transport=httpx.MockTransport(handler)
    )

    response = await transport.send(request)
    await transport.aclose()

    assert seen["url"] == "https://api.example.test/v1/chat/completions"
    assert seen["header_items"] == [
        ("host", "api.example.test"),
        ("content-type", "application/json"),
        ("user-agent", "millforge-model-backend/1"),
        ("authorization", "Bearer sk-real-secret"),
        ("content-length", str(seen["content_length"])),
    ]
    assert seen["timeout"] == {"connect": 5, "read": 5, "write": 5, "pool": 5}
    assert seen["body"] == expected_body
    assert response.provider_request_id == "provider-req-1"
    assert response.body["choices"][0]["message"]["tool_calls"][0]["id"] == "call-2"


@pytest.mark.asyncio
async def test_default_client_serializes_owned_messages_tools_and_canonical_arguments() -> (
    None
):
    response = TransportResponse(
        status_code=200,
        body={
            "model": "gpt-test",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
        },
    )
    client, transport = _client(response=response)
    request = _request(secret_refs=(_secret(),)).model_copy(
        update={
            "messages": (
                SystemMessage(content="be terse"),
                UserMessage(content="weather"),
                AssistantMessage(
                    tool_calls=(
                        ModelToolCall(
                            call_id="call-1",
                            name="lookup",
                            arguments=ParsedToolArguments(value={"b": 2, "a": 1}),
                        ),
                    )
                ),
                ToolResultMessage(
                    tool_call_id="call-1",
                    tool_name="lookup",
                    content="sunny",
                ),
            ),
            "tools": (
                ModelToolDefinition(
                    name="lookup",
                    description="Lookup weather",
                    input_schema={"type": "object"},
                ),
            ),
        }
    )

    await client.complete(request)

    assert transport.requests[0].body["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "weather"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "arguments": '{"a":1,"b":2}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "lookup",
            "content": "sunny",
        },
    ]
    assert transport.requests[0].body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Lookup weather",
                "parameters": {"type": "object"},
            },
        }
    ]


@pytest.mark.asyncio
async def test_default_client_rejects_non_finite_tool_arguments_before_encoding() -> (
    None
):
    client, transport = _client()
    request = _request(secret_refs=(_secret(),)).model_copy(
        update={
            "messages": (
                UserMessage(content="weather"),
                AssistantMessage(
                    tool_calls=(
                        ModelToolCall(
                            call_id="call-1",
                            name="lookup",
                            arguments=ParsedToolArguments(
                                value={"value": float("nan")}
                            ),
                        ),
                    )
                ),
            )
        }
    )

    with pytest.raises(ModelBackendConfigError, match="non-finite"):
        await client.complete(request)

    assert transport.requests == []


@pytest.mark.asyncio
async def test_openai_chat_transport_rejects_duplicate_keys_and_invalid_content_type() -> (
    None
):
    async def duplicate_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"model":"gpt-test","model":"other","choices":[]}',
        )

    transport = OpenAIChatCompletionsTransport(
        http_transport=httpx.MockTransport(duplicate_handler)
    )
    profile = _profile()
    request = TransportRequest(
        request_id="req-1",
        profile=profile,
        public_request=_request(secret_refs=(_secret(),)),
        url=profile.endpoint.chat_completions_url,
        headers={},
        body={"model": "gpt-test", "messages": [], "stream": False},
        timeout_seconds=5,
    )

    with pytest.raises(ModelProviderError) as exc_info:
        await transport.send(request)
    await transport.aclose()
    assert exc_info.value.category is ProviderErrorCategory.MALFORMED_RESPONSE

    async def html_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"{}",
        )

    invalid_type_transport = OpenAIChatCompletionsTransport(
        http_transport=httpx.MockTransport(html_handler)
    )
    with pytest.raises(ModelProviderError, match="content type"):
        await invalid_type_transport.send(request)
    await invalid_type_transport.aclose()


@pytest.mark.asyncio
async def test_openai_chat_transport_missing_content_type_requires_explicit_profile() -> (
    None
):
    async def missing_content_type_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                b'{"model":"gpt-test","choices":[{"finish_reason":"stop",'
                b'"message":{"role":"assistant","content":"ok"}}]}'
            ),
        )

    default_profile = _profile()
    request = TransportRequest(
        request_id="req-1",
        profile=default_profile,
        public_request=_request(secret_refs=(_secret(),)),
        url=default_profile.endpoint.chat_completions_url,
        headers={},
        body={"model": "gpt-test", "messages": [], "stream": False},
        timeout_seconds=5,
    )
    default_transport = OpenAIChatCompletionsTransport(
        http_transport=httpx.MockTransport(missing_content_type_handler)
    )

    with pytest.raises(ModelProviderError, match="content type"):
        await default_transport.send(request)
    await default_transport.aclose()

    opt_in_profile = _profile().model_copy(
        update={
            "endpoint": EndpointConfig(
                base_url="https://api.example.test/v1/",
                allow_missing_success_content_type=True,
            )
        }
    )
    opt_in_request = request.model_copy(
        update={
            "profile": opt_in_profile,
            "url": opt_in_profile.endpoint.chat_completions_url,
        }
    )
    opt_in_transport = OpenAIChatCompletionsTransport(
        http_transport=httpx.MockTransport(missing_content_type_handler)
    )

    response = await opt_in_transport.send(opt_in_request)
    await opt_in_transport.aclose()

    assert response.normalized_response == ModelCompletionResponse(
        provider_request_id=None,
        model_id="gpt-test",
        message=AssistantMessage(content="ok"),
        finish_reason="stop",
    )


@pytest.mark.asyncio
async def test_openai_chat_transport_rejects_invalid_content_type_when_missing_allowed() -> (
    None
):
    async def html_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"{}",
        )

    profile = _profile().model_copy(
        update={
            "endpoint": EndpointConfig(
                base_url="https://api.example.test/v1/",
                allow_missing_success_content_type=True,
            )
        }
    )
    request = TransportRequest(
        request_id="req-1",
        profile=profile,
        public_request=_request(secret_refs=(_secret(),)),
        url=profile.endpoint.chat_completions_url,
        headers={},
        body={"model": "gpt-test", "messages": [], "stream": False},
        timeout_seconds=5,
    )
    transport = OpenAIChatCompletionsTransport(
        http_transport=httpx.MockTransport(html_handler)
    )

    with pytest.raises(ModelProviderError, match="content type"):
        await transport.send(request)
    await transport.aclose()


@pytest.mark.asyncio
async def test_openai_chat_transport_rejects_strict_message_and_tool_call_shapes() -> (
    None
):
    async def wrong_role_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "model": "gpt-test",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "user", "content": "not assistant"},
                    }
                ],
            },
        )

    async def wrong_tool_type_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "model": "gpt-test",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "custom",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    profile = _profile()
    request = TransportRequest(
        request_id="req-1",
        profile=profile,
        public_request=_request(secret_refs=(_secret(),)),
        url=profile.endpoint.chat_completions_url,
        headers={},
        body={"model": "gpt-test", "messages": [], "stream": False},
        timeout_seconds=5,
    )

    for handler in (wrong_role_handler, wrong_tool_type_handler):
        transport = OpenAIChatCompletionsTransport(
            http_transport=httpx.MockTransport(handler)
        )
        with pytest.raises(ModelProviderError) as exc_info:
            await transport.send(request)
        await transport.aclose()
        assert exc_info.value.category is ProviderErrorCategory.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_openai_chat_transport_classifies_provider_errors_and_limits_body() -> (
    None
):
    async def unauthorized_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            headers={
                "content-type": "application/json",
                "x-request-id": "req-provider-2",
            },
            json={"error": {"message": "bad Bearer sk-secret-value", "code": "auth"}},
        )

    profile = _profile()
    request = TransportRequest(
        request_id="req-1",
        profile=profile,
        public_request=_request(secret_refs=(_secret(),)),
        url=profile.endpoint.chat_completions_url,
        headers={},
        body={"model": "gpt-test", "messages": [], "stream": False},
        timeout_seconds=5,
    )
    transport = OpenAIChatCompletionsTransport(
        http_transport=httpx.MockTransport(unauthorized_handler)
    )

    with pytest.raises(ModelProviderError) as exc_info:
        await transport.send(request)
    await transport.aclose()
    assert exc_info.value.category is ProviderErrorCategory.AUTHENTICATION
    assert exc_info.value.provider_request_id == "req-provider-2"
    assert "sk-secret-value" not in str(exc_info.value)
    assert isinstance(exc_info.value.fields, MappingProxyType)
    with pytest.raises(TypeError):
        exc_info.value.fields["unsafe"] = "mutated"  # type: ignore[index]

    for status_code, expected_category, expected_retryable in (
        (404, ProviderErrorCategory.UNKNOWN, False),
        (409, ProviderErrorCategory.INVALID_REQUEST, False),
    ):

        async def status_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code,
                headers={"content-type": "application/json"},
                json={"error": {"message": "provider conflict"}},
            )

        status_transport = OpenAIChatCompletionsTransport(
            http_transport=httpx.MockTransport(status_handler)
        )
        with pytest.raises(ModelProviderError) as status_error:
            await status_transport.send(request)
        await status_transport.aclose()
        assert status_error.value.category is expected_category
        assert status_error.value.retryable is expected_retryable

    async def oversized_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"x" * ((4 * 1024 * 1024) + 1),
        )

    limited_transport = OpenAIChatCompletionsTransport(
        http_transport=httpx.MockTransport(oversized_handler)
    )
    with pytest.raises(ModelProviderError, match="exceeded"):
        await limited_transport.send(request)
    await limited_transport.aclose()


def test_provider_error_sanitizes_bounded_provider_request_id_and_fields() -> None:
    error = ModelProviderError(
        category=ProviderErrorCategory.UNKNOWN,
        message="provider failed",
        provider_request_id="Bearer sk-secret-value",
        fields={f"field-{index}": f"value-{index}" for index in range(30)},
    )

    assert error.provider_request_id == "Bearer **redacted**"
    assert isinstance(error.fields, MappingProxyType)
    assert len(error.fields) == 24


@pytest.mark.asyncio
async def test_openai_chat_transport_maps_timeout_and_connection_failures() -> None:
    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    profile = _profile()
    request = TransportRequest(
        request_id="req-1",
        profile=profile,
        public_request=_request(secret_refs=(_secret(),)),
        url=profile.endpoint.chat_completions_url,
        headers={},
        body={"model": "gpt-test", "messages": [], "stream": False},
        timeout_seconds=5,
    )
    timeout_transport = OpenAIChatCompletionsTransport(
        http_transport=httpx.MockTransport(timeout_handler)
    )
    with pytest.raises(ModelProviderError) as timeout_error:
        await timeout_transport.send(request)
    await timeout_transport.aclose()
    assert timeout_error.value.category is ProviderErrorCategory.TIMEOUT

    async def connection_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    connection_transport = OpenAIChatCompletionsTransport(
        http_transport=httpx.MockTransport(connection_handler)
    )
    with pytest.raises(ModelProviderError) as connection_error:
        await connection_transport.send(request)
    await connection_transport.aclose()
    assert connection_error.value.category is ProviderErrorCategory.CONNECTION


@pytest.mark.asyncio
async def test_openai_chat_transport_owns_one_client_and_closes_without_new_tasks() -> (
    None
):
    seen_client_ids: list[int] = []
    task_snapshot = asyncio.all_tasks()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "model": "gpt-test",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
            },
        )

    transport = OpenAIChatCompletionsTransport(
        http_transport=httpx.MockTransport(handler)
    )
    profile = _profile()
    request = TransportRequest(
        request_id="req-1",
        profile=profile,
        public_request=_request(secret_refs=(_secret(),)),
        url=profile.endpoint.chat_completions_url,
        headers={"Content-Type": "application/json"},
        body={"model": "gpt-test", "messages": [], "stream": False},
        timeout_seconds=5,
    )

    seen_client_ids.append(id(transport._client))
    await transport.send(request)
    await transport.send(request)
    seen_client_ids.append(id(transport._client))
    tasks_before_close = asyncio.all_tasks()
    await transport.aclose()
    await transport.aclose()
    await asyncio.sleep(0)
    tasks_after_close = asyncio.all_tasks()

    assert seen_client_ids == [seen_client_ids[0], seen_client_ids[0]]
    assert transport._closed is True
    assert transport._client.is_closed is True
    assert tasks_before_close == task_snapshot
    assert tasks_after_close == task_snapshot

    pooled_transport = OpenAIChatCompletionsTransport()
    pool = getattr(pooled_transport._client._transport, "_pool")
    await pooled_transport.aclose()
    assert pooled_transport._client.is_closed is True
    assert pool._connections == []
    assert pool._requests == []


@pytest.mark.asyncio
async def test_transport_header_assembly_rejects_case_insensitive_duplicates() -> None:
    with pytest.raises(ValidationError, match="protected"):
        HeaderValuePolicy(values={"accept": "text/plain"})

    profile = _profile().model_copy(
        update={
            "authentication": AuthenticationPolicy(
                scheme=AuthenticationScheme.HEADER,
                secret_ref=_secret(),
                header_name="X-Dup",
                allowed_custom_header_names=("x-dup",),
            ),
            "configured_headers": HeaderValuePolicy(values={"x-dup": "configured"}),
        }
    )
    client, transport = _client(profile=profile)
    with pytest.raises(ModelBackendConfigError, match="duplicate transport header"):
        await client.complete(_request(secret_refs=(_secret(),)))
    assert transport.requests == []


def test_default_model_backend_orchestration_path_imports_httpx_only_for_transport() -> (
    None
):
    import millforge.model_backend as model_backend

    source = inspect.getsource(model_backend)
    assert "class OpenAIChatCompletionsTransport" in source
    assert "httpx.AsyncClient" in source
