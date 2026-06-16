"""Deterministic tool execution result, validation, and trace helpers."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal

from millforge import (
    ArtifactRef,
    IdempotencyClass,
    SideEffectCertainty,
    SideEffectClass,
    SideEffectRecord,
    TimingMetadata,
    ToolBindingRef,
    ToolExecutionResult,
    ToolExecutionStatus,
    ToolTraceDecision,
    ToolTraceDecisionRecord,
    ToolTraceIdempotency,
    ToolTraceRecord,
    ToolTraceSideEffectClass,
    canonical_json_serialize,
    redact_diagnostic_text,
    redact_diagnostic_value,
    RedactionPolicy,
)
from millforge.tools.registry import ToolOutputPolicy

MAX_MODEL_SUMMARY_UTF8 = 8192
_HOST_PATH_RE = re.compile(r"(?<![\w.-])(?:/[^\s:;,]+)+")


class ToolExecutionErrorCode(str, Enum):
    """Stable tool execution result categories."""

    INVALID_ARGUMENTS = "invalid_arguments"
    CAPABILITY_DENIED = "capability_denied"
    POLICY_DENIED = "policy_denied"
    PREREQUISITE_DENIED = "prerequisite_denied"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    IMPLEMENTATION_ERROR = "implementation_error"
    AMBIGUOUS_SIDE_EFFECT = "ambiguous_side_effect"
    OUTPUT_VALIDATION_FAILED = "output_validation_failed"
    TERMINAL_INTENT_INVALID = "terminal_intent_invalid"
    BINDING_MISMATCH = "binding_mismatch"


MODEL_CORRECTABLE_CODES = frozenset(
    {
        ToolExecutionErrorCode.INVALID_ARGUMENTS,
        ToolExecutionErrorCode.CAPABILITY_DENIED,
        ToolExecutionErrorCode.POLICY_DENIED,
        ToolExecutionErrorCode.PREREQUISITE_DENIED,
        ToolExecutionErrorCode.NOT_FOUND,
        ToolExecutionErrorCode.CONFLICT,
        ToolExecutionErrorCode.TIMEOUT,
        ToolExecutionErrorCode.CANCELLED,
        ToolExecutionErrorCode.TERMINAL_INTENT_INVALID,
    }
)


def canonical_sha256(value: Any) -> str:
    """Hash a JSON-compatible value in the project canonical format."""
    return hashlib.sha256(canonical_json_serialize(value).encode("utf-8")).hexdigest()


def redact_tool_value(value: Any, *, policy: RedactionPolicy | None = None) -> Any:
    """Redact and bound a value before trace persistence or model return."""
    redacted = redact_diagnostic_value(value, policy=policy)
    return _redact_host_paths(redacted)


def bounded_summary(
    value: Any,
    *,
    max_utf8: int = MAX_MODEL_SUMMARY_UTF8,
    policy: RedactionPolicy | None = None,
) -> str:
    """Return a redacted non-empty summary bounded by UTF-8 byte length."""
    if isinstance(value, str):
        text = _redact_host_paths(redact_diagnostic_text(value, policy=policy))
    else:
        text = canonical_json_serialize(redact_tool_value(value, policy=policy)).strip()
    if len(text.encode("utf-8")) > max_utf8:
        raw = text.encode("utf-8")[:max_utf8]
        text = raw.decode("utf-8", errors="ignore") + "[truncated]"
    return text or "[empty]"


def output_hash(value: Any, *, policy: RedactionPolicy | None = None) -> str:
    """Hash the safe redacted output value."""
    return canonical_sha256(redact_tool_value(value, policy=policy))


def validate_json_object_schema(
    value: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> str | None:
    """Validate the descriptor schema subset used by built-in tools."""
    return _validate_schema_value(value, schema, path="$")


def make_tool_result(
    *,
    call_id: str,
    status: ToolExecutionStatus,
    code: ToolExecutionErrorCode | None,
    summary: str,
    structured_data: Any,
    side_effect_class: SideEffectClass,
    idempotency: IdempotencyClass,
    side_effect_certainty: SideEffectCertainty,
    input_sha256: str,
    retryable: bool = False,
    artifact_refs: tuple[ArtifactRef, ...] = (),
    output_sha256: str | None = None,
    timing: TimingMetadata | None = None,
    side_effect_record: SideEffectRecord | None = None,
    output_policy: ToolOutputPolicy | None = None,
) -> ToolExecutionResult:
    """Build a bounded, redacted ``ToolExecutionResult``."""
    safe_output_policy = _output_redaction_policy(output_policy)
    summary_limit = (
        output_policy.max_summary_utf8
        if output_policy is not None
        else MAX_MODEL_SUMMARY_UTF8
    )
    summary_policy = _summary_redaction_policy(summary_limit)
    safe_data = redact_tool_value(structured_data, policy=safe_output_policy)
    safe_summary = bounded_summary(
        summary,
        max_utf8=summary_limit,
        policy=summary_policy,
    )
    safe_side_effect_record = (
        None
        if side_effect_record is None
        else side_effect_record.model_copy(
            update={
                "summary": bounded_summary(
                    side_effect_record.summary,
                    max_utf8=summary_limit,
                    policy=summary_policy,
                )
            }
        )
    )
    if code is None and output_sha256 is None:
        output_sha256 = canonical_sha256(safe_data)
    return ToolExecutionResult(
        call_id=call_id,
        status=status,
        summary=safe_summary,
        structured_data=safe_data,
        artifact_refs=artifact_refs,
        error_code=None if code is None else code.value,
        retryable=retryable,
        side_effect_class=side_effect_class,
        idempotency=idempotency,
        side_effect_certainty=side_effect_certainty,
        side_effect_record=safe_side_effect_record,
        input_sha256=input_sha256,
        output_sha256=output_sha256,
        timing=timing or zero_timing(),
    )


def sanitize_tool_execution_result(
    result: ToolExecutionResult,
    *,
    output_policy: ToolOutputPolicy | None = None,
    input_sha256: str | None = None,
) -> ToolExecutionResult:
    """Sanitize an implementation-produced result for model-visible return."""
    safe_output_policy = _output_redaction_policy(output_policy)
    summary_limit = (
        output_policy.max_summary_utf8
        if output_policy is not None
        else MAX_MODEL_SUMMARY_UTF8
    )
    summary_policy = _summary_redaction_policy(summary_limit)
    safe_data = redact_tool_value(result.structured_data, policy=safe_output_policy)
    safe_summary = bounded_summary(
        result.summary,
        max_utf8=summary_limit,
        policy=summary_policy,
    )
    safe_side_effect_record = (
        None
        if result.side_effect_record is None
        else result.side_effect_record.model_copy(
            update={
                "summary": bounded_summary(
                    result.side_effect_record.summary,
                    max_utf8=summary_limit,
                    policy=summary_policy,
                )
            }
        )
    )
    output_sha256 = result.output_sha256
    if result.status is ToolExecutionStatus.SUCCESS or output_sha256 is not None:
        output_sha256 = canonical_sha256(safe_data)
    update = {
        "summary": safe_summary,
        "structured_data": safe_data,
        "side_effect_record": safe_side_effect_record,
        "output_sha256": output_sha256,
    }
    if input_sha256 is not None:
        update["input_sha256"] = input_sha256
    return result.model_copy(update=update)


def make_denial_result(
    *,
    call_id: str,
    code: ToolExecutionErrorCode,
    summary: str,
    evidence: Mapping[str, Any],
    side_effect_class: SideEffectClass,
    idempotency: IdempotencyClass,
    input_sha256: str,
    status: ToolExecutionStatus | None = None,
) -> ToolExecutionResult:
    """Build a deterministic pre-entry denial result."""
    if status is None:
        hard = code not in MODEL_CORRECTABLE_CODES
        status = (
            ToolExecutionStatus.HARD_FAILURE
            if hard
            else ToolExecutionStatus.NOT_EXECUTED
        )
    return make_tool_result(
        call_id=call_id,
        status=status,
        code=code,
        summary=summary,
        structured_data={"category": code.value, "evidence": dict(evidence)},
        side_effect_class=side_effect_class,
        idempotency=idempotency,
        side_effect_certainty=SideEffectCertainty.NOT_ATTEMPTED,
        input_sha256=input_sha256,
        output_sha256=None,
    )


def make_trace_record(
    *,
    sequence: int,
    request_id: str,
    run_id: str,
    session_id: str,
    stage: Any,
    node_id: str,
    model_turn: int,
    tool_call_id: str,
    model_tool_name: str,
    binding: ToolBindingRef,
    binding_resolution_status: Literal[
        "resolved", "ambiguous", "uncompiled"
    ] = "resolved",
    input_sha256: str,
    prerequisite_decisions: Mapping[str, ToolTraceDecision],
    capability_decisions: Mapping[str, ToolTraceDecision],
    result: ToolExecutionResult,
    summary_max_utf8: int = MAX_MODEL_SUMMARY_UTF8,
    occurred_at: str = "1970-01-01T00:00:00+00:00",
    monotonic_offset_ms: float = 0.0,
) -> ToolTraceRecord:
    """Build and validate a redacted trace record for an attempted tool call."""
    record = result.side_effect_record
    summary_policy = RedactionPolicy(
        max_string_length=max(summary_max_utf8, 1),
        max_total_bytes=max(summary_max_utf8, 1),
    )
    return ToolTraceRecord(
        schema_version="1.0",
        sequence=sequence,
        occurred_at=occurred_at,
        monotonic_offset_ms=monotonic_offset_ms,
        request_id=request_id,
        run_id=run_id,
        session_id=session_id,
        stage=stage,
        node_id=node_id,
        model_turn=model_turn,
        tool_call_id=tool_call_id,
        model_tool_name=model_tool_name,
        binding=binding,
        binding_resolution_status=binding_resolution_status,
        input_sha256=input_sha256,
        prerequisite_decisions=tuple(
            ToolTraceDecisionRecord(key=key, decision=decision)
            for key, decision in sorted(prerequisite_decisions.items())
        ),
        capability_decisions=tuple(
            ToolTraceDecisionRecord(key=key, decision=decision)
            for key, decision in sorted(capability_decisions.items())
        ),
        execution_status=result.status,
        retryable=result.retryable,
        side_effect_class=ToolTraceSideEffectClass(result.side_effect_class.value),
        idempotency=ToolTraceIdempotency(result.idempotency.value),
        side_effect_certainty=result.side_effect_certainty,
        side_effect_detail_code=None if record is None else record.detail_code,
        side_effect_detail_summary=None
        if record is None
        else bounded_summary(
            record.summary,
            max_utf8=summary_max_utf8,
            policy=summary_policy,
        ),
        side_effect_retry_allowed=None if record is None else record.retry_allowed,
        output_sha256=result.output_sha256,
        duration_ms=result.duration_ms,
        summary=bounded_summary(
            result.summary,
            max_utf8=summary_max_utf8,
            policy=summary_policy,
        ),
    )


def zero_timing() -> TimingMetadata:
    """Return deterministic timing metadata for synchronous tests."""
    return TimingMetadata(
        started_at="1970-01-01T00:00:00+00:00",
        completed_at="1970-01-01T00:00:00+00:00",
        duration_ms=0.0,
    )


def _output_redaction_policy(
    output_policy: ToolOutputPolicy | None,
) -> RedactionPolicy | None:
    if output_policy is None:
        return None
    return RedactionPolicy(
        max_string_length=max(
            output_policy.max_output_bytes, output_policy.max_summary_utf8
        ),
        max_total_bytes=output_policy.max_output_bytes,
    )


def _summary_redaction_policy(summary_limit: int) -> RedactionPolicy:
    return RedactionPolicy(
        max_string_length=summary_limit,
        max_total_bytes=summary_limit,
    )


def _validate_schema_value(
    value: Any, schema: Mapping[str, Any], *, path: str
) -> str | None:
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, Mapping):
            return f"{path} must be object"
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                return f"{path}.{key} is required"
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                return f"{path}.{sorted(extra)[0]} is not allowed"
        for key, item in value.items():
            child_schema = properties.get(key)
            if child_schema is None:
                continue
            error = _validate_schema_value(item, child_schema, path=f"{path}.{key}")
            if error is not None:
                return error
    elif expected_type == "array":
        if not isinstance(value, list):
            return f"{path} must be array"
        item_schema = schema.get("items", {})
        for index, item in enumerate(value):
            error = _validate_schema_value(item, item_schema, path=f"{path}[{index}]")
            if error is not None:
                return error
    elif expected_type == "string":
        if not isinstance(value, str):
            return f"{path} must be string"
    elif expected_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return f"{path} must be integer"
    elif expected_type == "boolean":
        if not isinstance(value, bool):
            return f"{path} must be boolean"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {schema['enum']!r}"
    return None


def _redact_host_paths(value: Any) -> Any:
    if isinstance(value, str):
        return _HOST_PATH_RE.sub("[path]", value)
    if isinstance(value, Mapping):
        return {str(key): _redact_host_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_host_paths(item) for item in value]
    return value
