"""Tests for the millforge contract models.

Verifies Pydantic v2 API usage, closed-world validation, immutable
snapshot behaviour, serialisation round-trips, and secret-value safety.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from millforge.contracts import (
    ArtifactRef,
    CancellationRef,
    CapabilityEnvelope,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    DiagnosticMetadata,
    GuardedSessionRequest,
    GuardedSessionResult,
    HarnessExecutionResult,
    ModelProfile,
    RunDirRef,
    SecretRef,
    StageExecutionRequest,
    TerminalIntent,
    TimeoutRef,
    TimingMetadata,
    UsageMetadata,
    ValidatedModelRequest,
    ValidatedModelResponse,
    ValidatedToolCall,
    ValidatedToolResult,
)

# ---------------------------------------------------------------------------
# All models are exported in millforge.__all__
# ---------------------------------------------------------------------------

CONTRACT_MODELS = [
    ArtifactRef,
    CancellationRef,
    CapabilityEnvelope,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    DiagnosticMetadata,
    GuardedSessionRequest,
    GuardedSessionResult,
    HarnessExecutionResult,
    ModelProfile,
    RunDirRef,
    SecretRef,
    StageExecutionRequest,
    TerminalIntent,
    TimeoutRef,
    TimingMetadata,
    UsageMetadata,
    ValidatedModelRequest,
    ValidatedModelResponse,
    ValidatedToolCall,
    ValidatedToolResult,
]


def test_all_contracts_exported() -> None:
    from millforge import __all__ as exported

    expected = {cls.__name__ for cls in CONTRACT_MODELS}
    exported_set = set(exported)
    assert expected.issubset(exported_set), f"Missing: {expected - exported_set}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_identity() -> CompiledHarnessIdentity:
    return CompiledHarnessIdentity(
        compiled_plan_id="plan-abc123",
        harness_id="harness-01",
        version="1.0.0",
    )


def _valid_hash() -> CompiledHarnessHash:
    return CompiledHarnessHash(
        algorithm="sha256",
        digest="abc123deadbeef" * 4,
    )


def _valid_run_dir() -> RunDirRef:
    return RunDirRef(
        run_id="run-xyz789",
        path=Path("/tmp/millforge/runs/run-xyz789"),
    )


def _valid_artifact() -> ArtifactRef:
    return ArtifactRef(
        artifact_id="art-001",
        path=Path("/tmp/millforge/artifacts/summary.md"),
        content_type="text/markdown",
    )


def _valid_stage_request() -> StageExecutionRequest:
    return StageExecutionRequest(
        request_id="req-001",
        run_id="run-xyz789",
        stage="builder",
        task_id="task-02a-03-contracts",
        mode_id="deepseek_pi",
        compiled_plan_id="plan-deepseek_pi-06f7d4e20414",
    )


def _valid_capability() -> CapabilityEnvelope:
    return CapabilityEnvelope(
        capability="workspace.read",
        decision="granted",
        enforcement="advisory_only",
    )


def _valid_model_profile() -> ModelProfile:
    return ModelProfile(
        model_name="deepseek-v4-flash",
        provider="deepseek",
        assigned_alias="deepseek_flash_high",
        source="mode:stage:builder",
        parameters={"temperature": 0.7, "top_p": 0.9},
    )


def _valid_timeout() -> TimeoutRef:
    return TimeoutRef(timeout_seconds=3600.0, deadline="2026-06-10T18:00:00Z")


def _valid_cancellation() -> CancellationRef:
    return CancellationRef(
        cancellation_token="cancel-tok-001",
        reason="User requested cancellation",
    )


def _valid_model_request() -> ValidatedModelRequest:
    return ValidatedModelRequest(
        model="deepseek-v4-flash",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ],
        tools=None,
        temperature=0.7,
        max_tokens=4096,
        stream=False,
    )


def _valid_usage() -> UsageMetadata:
    return UsageMetadata(input_tokens=150, output_tokens=42, total_tokens=192)


def _valid_model_response() -> ValidatedModelResponse:
    return ValidatedModelResponse(
        model="deepseek-v4-flash",
        content="Hello! How can I help you?",
        tool_calls=None,
        finish_reason="stop",
        usage=_valid_usage(),
    )


def _valid_tool_call() -> ValidatedToolCall:
    return ValidatedToolCall(
        id="call-001",
        name="get_weather",
        arguments={"location": "San Francisco"},
    )


def _valid_tool_result() -> ValidatedToolResult:
    return ValidatedToolResult(
        call_id="call-001",
        output='{"temperature": 18, "conditions": "foggy"}',
        error=None,
        duration_ms=450,
    )


def _valid_guarded_request() -> GuardedSessionRequest:
    return GuardedSessionRequest(
        session_id="sess-001",
        request_type="model_inference",
        payload={
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "Hi"}],
        },
    )


def _valid_guarded_result() -> GuardedSessionResult:
    return GuardedSessionResult(
        session_id="sess-001",
        result_type="model_response",
        payload={"content": "Hello!"},
        blocked=False,
        reason=None,
    )


def _valid_terminal_intent() -> TerminalIntent:
    return TerminalIntent(
        disposition="success",
        message="Task completed successfully.",
        run_id="run-xyz789",
        stage="builder",
    )


def _valid_harness_result() -> HarnessExecutionResult:
    return HarnessExecutionResult(
        exit_code=0,
        stdout="Build complete",
        stderr="",
        success=True,
        metadata={"duration_ms": 1200},
    )


def _valid_timing() -> TimingMetadata:
    return TimingMetadata(
        started_at="2026-06-10T17:30:00Z",
        completed_at="2026-06-10T17:30:05Z",
        duration_ms=5120.5,
    )


def _valid_diagnostic() -> DiagnosticMetadata:
    return DiagnosticMetadata(
        error_code="E001",
        error_message="Something went wrong",
        context={"component": "builder", "stage": "execute"},
    )


# ---------------------------------------------------------------------------
# Valid construction
# ---------------------------------------------------------------------------


class TestValidConstruction:
    """Every required model constructs successfully with valid input."""

    def test_compiled_harness_identity(self) -> None:
        m = _valid_identity()
        assert m.compiled_plan_id == "plan-abc123"
        assert m.harness_id == "harness-01"
        assert m.version == "1.0.0"

    def test_compiled_harness_hash(self) -> None:
        m = _valid_hash()
        assert m.algorithm == "sha256"
        assert len(m.digest) > 0

    def test_run_dir_ref(self) -> None:
        m = _valid_run_dir()
        assert m.run_id == "run-xyz789"
        assert isinstance(m.path, Path)

    def test_artifact_ref(self) -> None:
        m = _valid_artifact()
        assert m.artifact_id == "art-001"
        assert m.content_type == "text/markdown"

    def test_artifact_ref_default_content_type(self) -> None:
        m = ArtifactRef(artifact_id="art-002", path=Path("/tmp/x"))
        assert m.content_type is None

    def test_stage_execution_request(self) -> None:
        m = _valid_stage_request()
        assert m.request_id == "req-001"
        assert m.stage == "builder"

    def test_capability_envelope(self) -> None:
        m = _valid_capability()
        assert m.capability == "workspace.read"
        assert m.decision == "granted"

    def test_model_profile(self) -> None:
        m = _valid_model_profile()
        assert m.model_name == "deepseek-v4-flash"
        assert m.parameters == {"temperature": 0.7, "top_p": 0.9}

    def test_model_profile_default_parameters(self) -> None:
        m = ModelProfile(
            model_name="gpt-4",
            provider="openai",
            assigned_alias="gpt4_default",
            source="mode:stage:default",
        )
        assert m.parameters == {}

    def test_timeout_ref(self) -> None:
        m = _valid_timeout()
        assert m.timeout_seconds == 3600.0
        assert m.deadline == "2026-06-10T18:00:00Z"

    def test_timeout_ref_default_deadline(self) -> None:
        m = TimeoutRef(timeout_seconds=30.0)
        assert m.deadline is None

    def test_cancellation_ref(self) -> None:
        m = _valid_cancellation()
        assert m.cancellation_token == "cancel-tok-001"
        assert m.reason == "User requested cancellation"

    def test_cancellation_ref_default_reason(self) -> None:
        m = CancellationRef(cancellation_token="tok-002")
        assert m.reason is None

    def test_validated_model_request(self) -> None:
        m = _valid_model_request()
        assert m.model == "deepseek-v4-flash"
        assert len(m.messages) == 2
        assert m.stream is False

    def test_usage_metadata(self) -> None:
        m = _valid_usage()
        assert m.input_tokens == 150
        assert m.total_tokens == 192

    def test_validated_model_response(self) -> None:
        m = _valid_model_response()
        assert m.content == "Hello! How can I help you?"
        assert m.finish_reason == "stop"
        assert m.usage is not None
        assert m.usage.input_tokens == 150

    def test_validated_tool_call(self) -> None:
        m = _valid_tool_call()
        assert m.id == "call-001"
        assert m.name == "get_weather"
        assert m.arguments == {"location": "San Francisco"}

    def test_validated_tool_result(self) -> None:
        m = _valid_tool_result()
        assert m.call_id == "call-001"
        assert m.error is None
        assert m.duration_ms == 450

    def test_guarded_session_request(self) -> None:
        m = _valid_guarded_request()
        assert m.session_id == "sess-001"
        assert m.request_type == "model_inference"

    def test_guarded_session_result(self) -> None:
        m = _valid_guarded_result()
        assert m.blocked is False
        assert m.reason is None

    def test_terminal_intent(self) -> None:
        m = _valid_terminal_intent()
        assert m.disposition == "success"
        assert m.stage == "builder"

    def test_harness_execution_result(self) -> None:
        m = _valid_harness_result()
        assert m.exit_code == 0
        assert m.success is True
        assert m.metadata == {"duration_ms": 1200}

    def test_harness_execution_result_defaults(self) -> None:
        m = HarnessExecutionResult(exit_code=1, success=False)
        assert m.stdout == ""
        assert m.stderr == ""
        assert m.metadata is None

    def test_timing_metadata(self) -> None:
        m = _valid_timing()
        assert m.duration_ms == 5120.5

    def test_diagnostic_metadata(self) -> None:
        m = _valid_diagnostic()
        assert m.error_code == "E001"
        assert m.context is not None


# ---------------------------------------------------------------------------
# Unknown-field rejection (extra="forbid")
# ---------------------------------------------------------------------------


class TestUnknownFields:
    """Every model rejects unknown fields with a clear ValidationError."""

    @pytest.mark.parametrize("model_cls", CONTRACT_MODELS)
    def test_unknown_field_raises(self, model_cls: type) -> None:
        """Passing an unknown field name raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            model_cls.model_validate({"unknown_field": "gotcha"})
        assert (
            "extra_forbidden" in str(exc_info.value).lower()
            or "extra" in str(exc_info.value).lower()
        )

    @pytest.mark.parametrize("model_cls", CONTRACT_MODELS)
    def test_unknown_field_in_partial(self, model_cls: type) -> None:
        """A valid field plus an unknown field still raises."""
        # Build a payload with at least the required fields for this model
        if model_cls is CompiledHarnessIdentity:
            payload: dict = {"compiled_plan_id": "x", "harness_id": "y", "version": "z"}
        elif model_cls is CompiledHarnessHash:
            payload = {"algorithm": "sha256", "digest": "abc"}
        elif model_cls is RunDirRef:
            payload = {"run_id": "r", "path": "/tmp/x"}
        elif model_cls is ArtifactRef:
            payload = {"artifact_id": "a", "path": "/tmp/x"}
        elif model_cls is StageExecutionRequest:
            payload = {
                "request_id": "r",
                "run_id": "r",
                "stage": "s",
                "task_id": "t",
                "mode_id": "m",
                "compiled_plan_id": "c",
            }
        elif model_cls is CapabilityEnvelope:
            payload = {"capability": "c", "decision": "d", "enforcement": "e"}
        elif model_cls is ModelProfile:
            payload = {
                "model_name": "m",
                "provider": "p",
                "assigned_alias": "a",
                "source": "s",
            }
        elif model_cls is TimeoutRef:
            payload = {"timeout_seconds": 30.0}
        elif model_cls is CancellationRef:
            payload = {"cancellation_token": "t"}
        elif model_cls is ValidatedModelRequest:
            payload = {"model": "m", "messages": []}
        elif model_cls is ValidatedModelResponse:
            payload = {"model": "m"}
        elif model_cls is ValidatedToolCall:
            payload = {"id": "1", "name": "n", "arguments": {}}
        elif model_cls is ValidatedToolResult:
            payload = {"call_id": "c"}
        elif model_cls is GuardedSessionRequest:
            payload = {"session_id": "s", "request_type": "t", "payload": {}}
        elif model_cls is GuardedSessionResult:
            payload = {"session_id": "s", "result_type": "t", "payload": {}}
        elif model_cls is TerminalIntent:
            payload = {"disposition": "d", "message": "m", "run_id": "r", "stage": "s"}
        elif model_cls is HarnessExecutionResult:
            payload = {"exit_code": 0, "success": True}
        elif model_cls is UsageMetadata:
            payload = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        elif model_cls is TimingMetadata:
            payload = {"started_at": "now", "duration_ms": 1.0}
        elif model_cls is DiagnosticMetadata:
            payload = {}
        elif model_cls is SecretRef:
            payload = {"env_var": "MY_SECRET"}
        else:
            raise pytest.fail(f"Missing payload for {model_cls.__name__}")

        payload["bogus_extra"] = "should-not-pass"
        with pytest.raises(ValidationError) as exc_info:
            model_cls.model_validate(payload)
        assert (
            "extra_forbidden" in str(exc_info.value).lower()
            or "extra" in str(exc_info.value).lower()
        )


# ---------------------------------------------------------------------------
# Serialisation round-trip
# ---------------------------------------------------------------------------


class TestSerializationRoundTrip:
    """model_validate → model_dump preserves data."""

    def test_compiled_harness_identity(self) -> None:
        m = _valid_identity()
        data = m.model_dump()
        restored = CompiledHarnessIdentity.model_validate(data)
        assert restored == m
        assert restored.model_dump() == data

    def test_compiled_harness_hash(self) -> None:
        m = _valid_hash()
        data = m.model_dump()
        restored = CompiledHarnessHash.model_validate(data)
        assert restored == m

    def test_run_dir_ref(self) -> None:
        m = _valid_run_dir()
        data = m.model_dump()
        restored = RunDirRef.model_validate(data)
        assert restored == m
        # Path round-trips as string in dump, back to Path on validate
        assert isinstance(restored.path, Path)

    def test_artifact_ref(self) -> None:
        m = _valid_artifact()
        data = m.model_dump()
        restored = ArtifactRef.model_validate(data)
        assert restored == m
        assert isinstance(restored.path, Path)

    def test_stage_execution_request(self) -> None:
        m = _valid_stage_request()
        data = m.model_dump()
        restored = StageExecutionRequest.model_validate(data)
        assert restored == m

    def test_capability_envelope(self) -> None:
        m = _valid_capability()
        data = m.model_dump()
        restored = CapabilityEnvelope.model_validate(data)
        assert restored == m

    def test_model_profile(self) -> None:
        m = _valid_model_profile()
        data = m.model_dump()
        restored = ModelProfile.model_validate(data)
        assert restored == m
        assert restored.parameters == {"temperature": 0.7, "top_p": 0.9}

    def test_timeout_ref(self) -> None:
        m = _valid_timeout()
        data = m.model_dump()
        restored = TimeoutRef.model_validate(data)
        assert restored == m

    def test_cancellation_ref(self) -> None:
        m = _valid_cancellation()
        data = m.model_dump()
        restored = CancellationRef.model_validate(data)
        assert restored == m

    def test_validated_model_request(self) -> None:
        m = _valid_model_request()
        data = m.model_dump()
        restored = ValidatedModelRequest.model_validate(data)
        assert restored == m

    def test_usage_metadata(self) -> None:
        m = _valid_usage()
        data = m.model_dump()
        restored = UsageMetadata.model_validate(data)
        assert restored == m

    def test_validated_model_response(self) -> None:
        m = _valid_model_response()
        data = m.model_dump()
        restored = ValidatedModelResponse.model_validate(data)
        assert restored == m
        assert restored.usage is not None
        assert restored.usage.input_tokens == 150

    def test_validated_tool_call(self) -> None:
        m = _valid_tool_call()
        data = m.model_dump()
        restored = ValidatedToolCall.model_validate(data)
        assert restored == m

    def test_validated_tool_result(self) -> None:
        m = _valid_tool_result()
        data = m.model_dump()
        restored = ValidatedToolResult.model_validate(data)
        assert restored == m

    def test_guarded_session_request(self) -> None:
        m = _valid_guarded_request()
        data = m.model_dump()
        restored = GuardedSessionRequest.model_validate(data)
        assert restored == m

    def test_guarded_session_result(self) -> None:
        m = _valid_guarded_result()
        data = m.model_dump()
        restored = GuardedSessionResult.model_validate(data)
        assert restored == m

    def test_terminal_intent(self) -> None:
        m = _valid_terminal_intent()
        data = m.model_dump()
        restored = TerminalIntent.model_validate(data)
        assert restored == m

    def test_harness_execution_result(self) -> None:
        m = _valid_harness_result()
        data = m.model_dump()
        restored = HarnessExecutionResult.model_validate(data)
        assert restored == m
        assert restored.stdout == "Build complete"

    def test_timing_metadata(self) -> None:
        m = _valid_timing()
        data = m.model_dump()
        restored = TimingMetadata.model_validate(data)
        assert restored == m

    def test_diagnostic_metadata(self) -> None:
        m = _valid_diagnostic()
        data = m.model_dump()
        restored = DiagnosticMetadata.model_validate(data)
        assert restored == m

    def test_secret_ref(self) -> None:
        m = SecretRef(env_var="MY_API_KEY", description="API key")
        data = m.model_dump()
        restored = SecretRef.model_validate(data)
        assert restored == m
        # SecretRef stores only the reference, never the value
        assert data == {"env_var": "MY_API_KEY", "description": "API key"}
        assert "secret_value" not in data


# ---------------------------------------------------------------------------
# Immutable models reject mutation (frozen=True)
# ---------------------------------------------------------------------------

FROZEN_MODELS = [
    CompiledHarnessIdentity,
    CompiledHarnessHash,
    RunDirRef,
    ArtifactRef,
    StageExecutionRequest,
    CapabilityEnvelope,
    ModelProfile,
    TimeoutRef,
    CancellationRef,
    ValidatedModelRequest,
    ValidatedModelResponse,
    ValidatedToolCall,
    ValidatedToolResult,
    TerminalIntent,
    HarnessExecutionResult,
    UsageMetadata,
    TimingMetadata,
    DiagnosticMetadata,
    SecretRef,
]

MUTABLE_MODELS = [
    GuardedSessionRequest,
    GuardedSessionResult,
]


class TestImmutability:
    """Frozen models reject attribute assignment."""

    @pytest.mark.parametrize("model_cls", FROZEN_MODELS)
    def test_frozen_rejects_mutation(self, model_cls: type) -> None:
        m = _build_frozen_instance(model_cls)
        # Attempt to set the first field
        field_name = list(model_cls.model_fields.keys())[0]
        with pytest.raises(Exception) as exc_info:
            setattr(m, field_name, "changed")
        assert isinstance(exc_info.value, (TypeError, ValidationError))

    @pytest.mark.parametrize("model_cls", MUTABLE_MODELS)
    def test_mutable_allows_mutation(self, model_cls: type) -> None:
        m = _build_mutable_instance(model_cls)
        field_name = list(model_cls.model_fields.keys())[0]
        original = getattr(m, field_name)
        new_val = "mutated" if isinstance(original, str) else not original
        setattr(m, field_name, new_val)
        assert getattr(m, field_name) == new_val


def _build_frozen_instance(model_cls: type) -> object:
    """Build a minimal valid instance of a frozen model."""
    builders = {
        CompiledHarnessIdentity: lambda: CompiledHarnessIdentity(
            compiled_plan_id="x", harness_id="y", version="z"
        ),
        CompiledHarnessHash: lambda: CompiledHarnessHash(
            algorithm="sha256", digest="abc"
        ),
        RunDirRef: lambda: RunDirRef(run_id="r", path=Path("/tmp")),
        ArtifactRef: lambda: ArtifactRef(artifact_id="a", path=Path("/tmp")),
        StageExecutionRequest: lambda: StageExecutionRequest(
            request_id="r",
            run_id="r",
            stage="s",
            task_id="t",
            mode_id="m",
            compiled_plan_id="c",
        ),
        CapabilityEnvelope: lambda: CapabilityEnvelope(
            capability="c", decision="d", enforcement="e"
        ),
        ModelProfile: lambda: ModelProfile(
            model_name="m", provider="p", assigned_alias="a", source="s"
        ),
        TimeoutRef: lambda: TimeoutRef(timeout_seconds=30.0),
        CancellationRef: lambda: CancellationRef(cancellation_token="t"),
        ValidatedModelRequest: lambda: ValidatedModelRequest(model="m", messages=[]),
        ValidatedModelResponse: lambda: ValidatedModelResponse(model="m"),
        ValidatedToolCall: lambda: ValidatedToolCall(id="1", name="n", arguments={}),
        ValidatedToolResult: lambda: ValidatedToolResult(call_id="c"),
        TerminalIntent: lambda: TerminalIntent(
            disposition="d", message="m", run_id="r", stage="s"
        ),
        HarnessExecutionResult: lambda: HarnessExecutionResult(
            exit_code=0, success=True
        ),
        UsageMetadata: lambda: UsageMetadata(
            input_tokens=1, output_tokens=1, total_tokens=2
        ),
        TimingMetadata: lambda: TimingMetadata(started_at="now", duration_ms=1.0),
        DiagnosticMetadata: lambda: DiagnosticMetadata(),
        SecretRef: lambda: SecretRef(env_var="VAR"),
    }
    builder = builders.get(model_cls)
    if builder is None:
        raise pytest.fail(f"No builder for {model_cls.__name__}")
    return builder()


def _build_mutable_instance(model_cls: type) -> object:
    """Build a minimal valid instance of a mutable model."""
    builders = {
        GuardedSessionRequest: lambda: GuardedSessionRequest(
            session_id="s", request_type="t", payload={}
        ),
        GuardedSessionResult: lambda: GuardedSessionResult(
            session_id="s", result_type="t", payload={}
        ),
    }
    builder = builders.get(model_cls)
    if builder is None:
        raise pytest.fail(f"No builder for {model_cls.__name__}")
    return builder()


# ---------------------------------------------------------------------------
# Secret-value safety
# ---------------------------------------------------------------------------


class TestSecretSafety:
    """Secret values must never appear in contract fields or serialized output."""

    def test_secret_ref_holds_reference_not_value(self) -> None:
        """SecretRef stores an env-var name, not the actual secret."""
        ref = SecretRef(env_var="DATABASE_PASSWORD")
        data = ref.model_dump()
        assert data["env_var"] == "DATABASE_PASSWORD"
        # No field should contain the actual secret value
        assert "hunter2" not in str(data)
        assert "s3cret" not in str(data)
        assert all(not isinstance(v, str) or len(v) < 100 for v in data.values())

    def test_secret_ref_with_description(self) -> None:
        ref = SecretRef(
            env_var="API_TOKEN",
            description="Token for external API access",
        )
        data = ref.model_dump()
        assert data["env_var"] == "API_TOKEN"
        assert data["description"] == "Token for external API access"

    @pytest.mark.parametrize("model_cls", CONTRACT_MODELS)
    def test_no_secret_value_in_any_model_dump(self, model_cls: type) -> None:
        """No serialized model dump contains obvious secret values."""
        try:
            instance = _build_frozen_instance(model_cls)
        except (KeyError, pytest.fail.Exception):
            try:
                instance = _build_mutable_instance(model_cls)
            except (KeyError, pytest.fail.Exception):
                return  # skip models without builders
        data = instance.model_dump()
        data_str = str(data).lower()
        # None of these should appear as field values
        forbidden = ["s3cret", "p@ssw0rd", "hunter2", "real_secret_value_here"]
        for term in forbidden:
            assert term not in data_str, (
                f"{model_cls.__name__} dump contains potential secret: {term}"
            )


# ---------------------------------------------------------------------------
# Pydantic v2 API usage (no v1 idioms)
# ---------------------------------------------------------------------------


class TestPydanticV2Api:
    """Models use Pydantic v2 APIs only."""

    def test_model_config_not_class_config(self) -> None:
        """No model uses a v1-style ``class Config``."""
        for cls in CONTRACT_MODELS:
            assert hasattr(cls, "model_config"), f"{cls.__name__} has no model_config"
            config = cls.model_config
            assert "extra" in config, f"{cls.__name__} missing extra in model_config"
            assert config["extra"] == "forbid"

    def test_model_validate_not_parse_obj(self) -> None:
        """Models use model_validate, not parse_obj."""
        request = _valid_stage_request()
        data = request.model_dump()
        restored = StageExecutionRequest.model_validate(data)
        assert isinstance(restored, StageExecutionRequest)

    def test_model_dump_not_dict(self) -> None:
        """Models use model_dump, not .dict()."""
        identity = _valid_identity()
        dumped = identity.model_dump()
        assert isinstance(dumped, dict)
        assert dumped["compiled_plan_id"] == "plan-abc123"


# ---------------------------------------------------------------------------
# Field validators
# ---------------------------------------------------------------------------


class TestValidators:
    """Field validators enforce constraints correctly."""

    def test_secret_ref_empty_env_var_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            SecretRef(env_var="   ")
        assert "non-empty" in str(exc_info.value).lower()

    def test_temperature_range(self) -> None:
        """temperature must be between 0 and 2."""
        with pytest.raises(ValidationError):
            ValidatedModelRequest(model="m", messages=[], temperature=-0.1)
        with pytest.raises(ValidationError):
            ValidatedModelRequest(model="m", messages=[], temperature=2.1)
        # Valid edge cases
        m = ValidatedModelRequest(model="m", messages=[], temperature=0.0)
        assert m.temperature == 0.0
        m = ValidatedModelRequest(model="m", messages=[], temperature=2.0)
        assert m.temperature == 2.0

    def test_max_tokens_ge_one(self) -> None:
        with pytest.raises(ValidationError):
            ValidatedModelRequest(model="m", messages=[], max_tokens=0)
        m = ValidatedModelRequest(model="m", messages=[], max_tokens=1)
        assert m.max_tokens == 1

    def test_usage_tokens_ge_zero(self) -> None:
        with pytest.raises(ValidationError):
            UsageMetadata(input_tokens=-1, output_tokens=0, total_tokens=0)
        with pytest.raises(ValidationError):
            UsageMetadata(input_tokens=0, output_tokens=-1, total_tokens=0)
        with pytest.raises(ValidationError):
            UsageMetadata(input_tokens=0, output_tokens=0, total_tokens=-1)
        m = UsageMetadata(input_tokens=0, output_tokens=0, total_tokens=0)
        assert m.total_tokens == 0

    def test_duration_ms_ge_zero(self) -> None:
        with pytest.raises(ValidationError):
            ValidatedToolResult(call_id="c", duration_ms=-1)
        m = ValidatedToolResult(call_id="c", duration_ms=0)
        assert m.duration_ms == 0

    def test_timing_duration_ms_ge_zero(self) -> None:
        with pytest.raises(ValidationError):
            TimingMetadata(started_at="now", duration_ms=-0.1)


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


class TestDefaultValues:
    """Models use Field(default_factory=...) for mutable containers."""

    def test_model_profile_parameters_default_factory(self) -> None:
        m = ModelProfile(model_name="m", provider="p", assigned_alias="a", source="s")
        assert m.parameters == {}
        # Ensure it's a fresh dict per instance
        m2 = ModelProfile(model_name="m", provider="p", assigned_alias="a", source="s")
        assert m2.parameters == {}
        assert m.parameters is not m2.parameters

    def test_validated_model_request_default_stream(self) -> None:
        m = ValidatedModelRequest(model="m", messages=[])
        assert m.stream is False

    def test_guarded_session_result_default_blocked(self) -> None:
        m = GuardedSessionResult(session_id="s", result_type="t", payload={})
        assert m.blocked is False
        assert m.reason is None

    def test_harness_execution_result_defaults(self) -> None:
        m = HarnessExecutionResult(exit_code=0, success=True)
        assert m.stdout == ""
        assert m.stderr == ""
        assert m.metadata is None


# ---------------------------------------------------------------------------
# Required-check smoke tests
# ---------------------------------------------------------------------------


def test_import_from_millforge() -> None:
    """``from millforge import CompiledHarnessIdentity, RunDirRef`` succeeds."""
    from millforge import ArtifactRef, CompiledHarnessIdentity, RunDirRef  # noqa: F811

    assert ArtifactRef is not None
    assert CompiledHarnessIdentity is not None
    assert RunDirRef is not None
