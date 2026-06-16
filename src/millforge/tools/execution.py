"""Compiled-plan scoped tool binding resolution and execution."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from enum import Enum
from typing import Any, Literal

from millforge import (
    ToolBindingRef,
    SideEffectCertainty,
    SideEffectClass,
    ToolExecutionStatus,
    ToolTraceDecision,
    canonical_json_serialize,
)
from millforge.compiled_plan import CompiledHarnessNode, CompiledHarnessPlan
from millforge.contracts import (
    SideEffectRecord,
    ToolExecutionContext,
    ToolExecutionResult,
    ValidatedToolCall,
)
from millforge.compiler.catalogs import ToolCatalogSnapshot
from millforge.tools.builtins import iter_builtin_tool_descriptors
from millforge.tools.registry import ToolOutputPolicy
from millforge.tools.results import (
    MAX_MODEL_SUMMARY_UTF8,
    ToolExecutionErrorCode,
    canonical_sha256,
    make_denial_result,
    make_tool_result,
    make_trace_record,
    sanitize_tool_execution_result,
    validate_json_object_schema,
)

RuntimeToolImplementation = Callable[
    [ValidatedToolCall, ToolExecutionContext],
    ToolExecutionResult | Awaitable[ToolExecutionResult],
]


class ToolBindingDenialCode(str, Enum):
    """Stable binding-denial categories."""

    BINDING_MISMATCH = "binding_mismatch"
    CONFLICT = "conflict"
    INVALID_ARGUMENTS = "invalid_arguments"
    NOT_FOUND = "not_found"


class RuntimeToolRegistry:
    """Explicit in-process registry of source-owned runtime implementations."""

    def __init__(self) -> None:
        self._implementations: dict[str, RuntimeToolImplementation] = {}

    def register(
        self,
        implementation_id: str,
        implementation: RuntimeToolImplementation,
    ) -> None:
        if not implementation_id.strip():
            raise ValueError("implementation_id must be non-empty")
        if not callable(implementation):
            raise TypeError("implementation must be callable")
        if implementation_id in self._implementations:
            raise ValueError(f"duplicate implementation_id {implementation_id!r}")
        self._implementations[implementation_id] = implementation

    def resolve(self, implementation_id: str) -> RuntimeToolImplementation | None:
        return self._implementations.get(implementation_id)


class CompiledToolBindingExecutor:
    """Tool executor admitted by exact compiled-plan and descriptor-snapshot bindings."""

    def __init__(
        self,
        *,
        plan: CompiledHarnessPlan,
        descriptor_snapshot: ToolCatalogSnapshot,
        runtime_registry: RuntimeToolRegistry,
    ) -> None:
        self._nodes_by_id = {node.node_id: node for node in plan.nodes}
        self._node_by_model_name: dict[str, CompiledHarnessNode] = {}
        self._terminal_result_map = dict(plan.terminal_result_map)
        self._conflicting_model_names = _duplicate_values(
            node.model_tool_name for node in plan.nodes
        )
        for node in plan.nodes:
            if node.model_tool_name not in self._conflicting_model_names:
                self._node_by_model_name[node.model_tool_name] = node
        self._node_defects = {
            node.node_id: _node_binding_defect(
                node=node,
                descriptor_snapshot=descriptor_snapshot,
                runtime_registry=runtime_registry,
            )
            for node in plan.nodes
        }
        self._descriptor_snapshot = descriptor_snapshot
        self._runtime_registry = runtime_registry
        self._trace_records: list[Any] = []
        self._next_sequence = 1

    def supports_tool(self, name: str) -> bool:
        node = self._node_by_model_name.get(name)
        return node is not None and self._node_defects[node.node_id] is None

    @property
    def trace_records(self) -> tuple[Any, ...]:
        """Return emitted trace records in execution order."""
        return tuple(self._trace_records)

    def validate_model_tool_call(
        self,
        *,
        model_tool_name: str,
        call_id: str,
        arguments: Mapping[str, Any],
    ) -> ValidatedToolCall | ToolExecutionResult:
        """Resolve a model-visible tool name into an exact compiled binding."""
        input_sha256 = canonical_sha256(arguments)
        if model_tool_name in self._conflicting_model_names:
            return _denial_result(
                call_id=call_id,
                input_sha256=input_sha256,
                code=ToolBindingDenialCode.CONFLICT,
                summary="model-visible tool name is ambiguous",
                evidence={"model_tool_name": model_tool_name},
            )
        node = self._node_by_model_name.get(model_tool_name)
        if node is None:
            return _denial_result(
                call_id=call_id,
                input_sha256=input_sha256,
                code=ToolBindingDenialCode.NOT_FOUND,
                summary="model-visible tool name is not compiled",
                evidence={"model_tool_name": model_tool_name},
            )
        defect = self._node_defects[node.node_id]
        if defect is not None:
            code, summary, evidence, hard_failure = defect
            return _denial_result(
                call_id=call_id,
                input_sha256=input_sha256,
                code=code,
                summary=summary,
                evidence={"node_id": node.node_id, **evidence},
                node=node,
                status=(ToolExecutionStatus.HARD_FAILURE if hard_failure else None),
            )
        input_error = validate_json_object_schema(arguments, node.input_schema)
        if input_error is not None:
            return _denial_result(
                call_id=call_id,
                input_sha256=input_sha256,
                code=ToolBindingDenialCode.INVALID_ARGUMENTS,
                summary="tool arguments failed descriptor input schema validation",
                evidence={"schema_error": input_error},
                node=node,
            )
        return ValidatedToolCall(
            call_id=call_id,
            node_id=node.node_id,
            binding=node.binding,
            arguments=dict(arguments),
        )

    async def execute_model_tool(
        self,
        *,
        model_tool_name: str,
        call_id: str,
        arguments: Mapping[str, Any],
        context: ToolExecutionContext,
        prerequisite_results: Mapping[str, ToolExecutionResult] | None = None,
        model_turn: int = 0,
        session_id: str | None = None,
    ) -> ToolExecutionResult:
        resolved = self.validate_model_tool_call(
            model_tool_name=model_tool_name,
            call_id=call_id,
            arguments=arguments,
        )
        if isinstance(resolved, ToolExecutionResult):
            node = self._node_by_model_name.get(model_tool_name)
            if node is not None:
                self._record_trace(
                    node=node,
                    call_id=call_id,
                    model_tool_name=model_tool_name,
                    input_sha256=resolved.input_sha256,
                    result=resolved,
                    context=context,
                    prerequisite_decisions={},
                    capability_decisions={},
                    model_turn=model_turn,
                    session_id=session_id,
                )
            else:
                binding_resolution_status: Literal["ambiguous", "uncompiled"] = (
                    "ambiguous"
                    if model_tool_name in self._conflicting_model_names
                    else "uncompiled"
                )
                self._record_trace(
                    node=None,
                    call_id=call_id,
                    model_tool_name=model_tool_name,
                    input_sha256=resolved.input_sha256,
                    result=resolved,
                    context=context,
                    prerequisite_decisions={},
                    capability_decisions={},
                    model_turn=model_turn,
                    session_id=session_id,
                    binding_resolution_status=binding_resolution_status,
                )
            return resolved
        return await self.execute(
            resolved,
            context,
            prerequisite_results=prerequisite_results,
            model_turn=model_turn,
            model_tool_name=model_tool_name,
            session_id=session_id,
        )

    async def execute(
        self,
        call: ValidatedToolCall,
        context: ToolExecutionContext,
        prerequisite_results: Mapping[str, ToolExecutionResult] | None = None,
        model_turn: int = 0,
        model_tool_name: str | None = None,
        session_id: str | None = None,
    ) -> ToolExecutionResult:
        input_sha256 = canonical_sha256(call.arguments)
        node = self._nodes_by_id.get(call.node_id)
        if node is None:
            return _denial_result(
                call_id=call.call_id,
                input_sha256=input_sha256,
                code=ToolBindingDenialCode.NOT_FOUND,
                summary="compiled node is not admitted",
                evidence={"node_id": call.node_id},
            )
        mismatch = _call_binding_mismatch(call, node)
        if mismatch is not None:
            result = _denial_result(
                call_id=call.call_id,
                input_sha256=input_sha256,
                code=ToolBindingDenialCode.BINDING_MISMATCH,
                summary="validated call binding does not match compiled node",
                evidence={"node_id": node.node_id, **mismatch},
                node=node,
            )
            self._record_trace(
                node=node,
                call_id=call.call_id,
                model_tool_name=model_tool_name or node.model_tool_name,
                input_sha256=input_sha256,
                result=result,
                context=context,
                prerequisite_decisions={},
                capability_decisions={},
                model_turn=model_turn,
                session_id=session_id,
            )
            return result
        defect = self._node_defects[node.node_id]
        if defect is not None:
            code, summary, evidence, hard_failure = defect
            result = _denial_result(
                call_id=call.call_id,
                input_sha256=input_sha256,
                code=code,
                summary=summary,
                evidence={"node_id": node.node_id, **evidence},
                node=node,
                status=(ToolExecutionStatus.HARD_FAILURE if hard_failure else None),
            )
            self._record_trace(
                node=node,
                call_id=call.call_id,
                model_tool_name=model_tool_name or node.model_tool_name,
                input_sha256=input_sha256,
                result=result,
                context=context,
                prerequisite_decisions={},
                capability_decisions={},
                model_turn=model_turn,
                session_id=session_id,
            )
            return result
        implementation = self._runtime_registry.resolve(node.binding.implementation_id)
        if implementation is None:
            return _denial_result(
                call_id=call.call_id,
                input_sha256=input_sha256,
                code=ToolBindingDenialCode.NOT_FOUND,
                summary="runtime implementation is not registered",
                evidence={
                    "node_id": node.node_id,
                    "implementation_id": node.binding.implementation_id,
                },
                node=node,
            )
        preflight = self._preflight_result(
            node=node,
            call=call,
            context=context,
            prerequisite_results=prerequisite_results or {},
            input_sha256=input_sha256,
        )
        if preflight is not None:
            result, prerequisite_decisions, capability_decisions = preflight
            self._record_trace(
                node=node,
                call_id=call.call_id,
                model_tool_name=model_tool_name or node.model_tool_name,
                input_sha256=input_sha256,
                result=result,
                context=context,
                prerequisite_decisions=prerequisite_decisions,
                capability_decisions=capability_decisions,
                model_turn=model_turn,
                session_id=session_id,
            )
            return result
        prerequisite_decisions = {
            prereq.node_id: "allowed" for prereq in node.prerequisites
        }
        capability_decisions = {
            capability: "allowed" for capability in node.required_capabilities
        }
        try:
            pending = implementation(call, context)
            result = await pending if inspect.isawaitable(pending) else pending
        except Exception as exc:
            result = make_tool_result(
                call_id=call.call_id,
                status=ToolExecutionStatus.HARD_FAILURE,
                code=ToolExecutionErrorCode.IMPLEMENTATION_ERROR,
                summary="tool implementation raised an exception",
                structured_data={
                    "category": ToolExecutionErrorCode.IMPLEMENTATION_ERROR.value,
                    "error_type": type(exc).__name__,
                    "error": exc,
                },
                side_effect_class=node.side_effect_class,
                idempotency=node.idempotency,
                side_effect_certainty=SideEffectCertainty.COMPLETION_UNKNOWN,
                side_effect_record=SideEffectRecord(
                    certainty=SideEffectCertainty.COMPLETION_UNKNOWN,
                    detail_code=ToolExecutionErrorCode.AMBIGUOUS_SIDE_EFFECT.value,
                    summary="implementation failure left completion unknown",
                    retry_allowed=False,
                ),
                input_sha256=input_sha256,
                retryable=False,
            )
        else:
            result = self._validate_implementation_result(
                node=node,
                call=call,
                result=result,
                input_sha256=input_sha256,
            )
        self._record_trace(
            node=node,
            call_id=call.call_id,
            model_tool_name=model_tool_name or node.model_tool_name,
            input_sha256=input_sha256,
            result=result,
            context=context,
            prerequisite_decisions=prerequisite_decisions,
            capability_decisions=capability_decisions,
            model_turn=model_turn,
            session_id=session_id,
        )
        return result

    def _preflight_result(
        self,
        *,
        node: CompiledHarnessNode,
        call: ValidatedToolCall,
        context: ToolExecutionContext,
        prerequisite_results: Mapping[str, ToolExecutionResult],
        input_sha256: str,
    ) -> (
        tuple[
            ToolExecutionResult,
            dict[str, str],
            dict[str, str],
        ]
        | None
    ):
        prerequisite_decisions: dict[str, str] = {}
        for prereq in node.prerequisites:
            prereq_result = prerequisite_results.get(prereq.node_id)
            if (
                prereq_result is None
                or prereq_result.status is not ToolExecutionStatus.SUCCESS
            ):
                prerequisite_decisions[prereq.node_id] = "denied"
                return (
                    make_denial_result(
                        call_id=call.call_id,
                        code=ToolExecutionErrorCode.PREREQUISITE_DENIED,
                        summary="required prerequisite has not completed successfully",
                        evidence={"prerequisite_node_id": prereq.node_id},
                        side_effect_class=node.side_effect_class,
                        idempotency=node.idempotency,
                        input_sha256=input_sha256,
                    ),
                    prerequisite_decisions,
                    {},
                )
            prerequisite_decisions[prereq.node_id] = "allowed"
        granted = {grant.capability_id for grant in context.capability_envelope.grants}
        capability_decisions: dict[str, str] = {}
        for capability in node.required_capabilities:
            if capability not in granted:
                capability_decisions[capability] = "denied"
                return (
                    make_denial_result(
                        call_id=call.call_id,
                        code=ToolExecutionErrorCode.CAPABILITY_DENIED,
                        summary="required capability is absent from request envelope",
                        evidence={"capability_id": capability},
                        side_effect_class=node.side_effect_class,
                        idempotency=node.idempotency,
                        input_sha256=input_sha256,
                    ),
                    prerequisite_decisions,
                    capability_decisions,
                )
            capability_decisions[capability] = "allowed"
        policy_result = _builtin_pre_entry_policy_result(call, context, input_sha256)
        if policy_result is not None:
            return policy_result, prerequisite_decisions, capability_decisions
        if node.terminal_result is not None:
            mapped_terminal_result = self._terminal_result_map.get(node.node_id)
            if mapped_terminal_result != node.terminal_result:
                return (
                    make_denial_result(
                        call_id=call.call_id,
                        code=ToolExecutionErrorCode.TERMINAL_INTENT_INVALID,
                        summary="compiled terminal result map does not match terminal node",
                        evidence={
                            "node_id": node.node_id,
                            "expected": node.terminal_result,
                            "actual": str(mapped_terminal_result),
                        },
                        side_effect_class=node.side_effect_class,
                        idempotency=node.idempotency,
                        input_sha256=input_sha256,
                    ),
                    prerequisite_decisions,
                    capability_decisions,
                )
            terminal_result = call.arguments.get("terminal_result")
            if terminal_result != node.terminal_result:
                return (
                    make_denial_result(
                        call_id=call.call_id,
                        code=ToolExecutionErrorCode.TERMINAL_INTENT_INVALID,
                        summary="terminal intent does not match compiled terminal result",
                        evidence={
                            "expected": node.terminal_result,
                            "actual": str(terminal_result),
                        },
                        side_effect_class=node.side_effect_class,
                        idempotency=node.idempotency,
                        input_sha256=input_sha256,
                    ),
                    prerequisite_decisions,
                    capability_decisions,
                )
        if context.deadline.remaining(lambda: context.current_monotonic) <= 0:
            return (
                make_denial_result(
                    call_id=call.call_id,
                    code=ToolExecutionErrorCode.TIMEOUT,
                    summary="tool deadline expired before implementation entry",
                    evidence={"deadline": "expired"},
                    side_effect_class=node.side_effect_class,
                    idempotency=node.idempotency,
                    input_sha256=input_sha256,
                ),
                prerequisite_decisions,
                capability_decisions,
            )
        if context.cancellation_requested:
            return (
                make_denial_result(
                    call_id=call.call_id,
                    code=ToolExecutionErrorCode.CANCELLED,
                    summary="tool call was cancelled before implementation entry",
                    evidence={"cancellation_id": context.cancellation.cancellation_id},
                    side_effect_class=node.side_effect_class,
                    idempotency=node.idempotency,
                    input_sha256=input_sha256,
                ),
                prerequisite_decisions,
                capability_decisions,
            )
        return None

    def _validate_implementation_result(
        self,
        *,
        node: CompiledHarnessNode,
        call: ValidatedToolCall,
        result: ToolExecutionResult,
        input_sha256: str,
    ) -> ToolExecutionResult:
        lookup = self._descriptor_snapshot.resolve_exact(
            node.binding.tool_id,
            node.binding.tool_version,
        )
        output_schema = lookup.entry.output_schema if lookup.entry is not None else {}
        output_error = validate_json_object_schema(
            result.structured_data
            if isinstance(result.structured_data, Mapping)
            else {},
            output_schema,
        )
        if output_error is not None:
            return make_tool_result(
                call_id=call.call_id,
                status=ToolExecutionStatus.HARD_FAILURE,
                code=ToolExecutionErrorCode.OUTPUT_VALIDATION_FAILED,
                summary="tool output failed descriptor output schema validation",
                structured_data={
                    "category": ToolExecutionErrorCode.OUTPUT_VALIDATION_FAILED.value,
                    "schema_error": output_error,
                },
                side_effect_class=node.side_effect_class,
                idempotency=node.idempotency,
                side_effect_certainty=result.side_effect_certainty,
                side_effect_record=result.side_effect_record,
                input_sha256=input_sha256,
                retryable=False,
            )
        output_policy = self._output_policy_for_node(node)
        return sanitize_tool_execution_result(
            result,
            output_policy=output_policy,
            input_sha256=input_sha256,
        )

    def _record_trace(
        self,
        *,
        node: CompiledHarnessNode | None,
        call_id: str,
        model_tool_name: str,
        input_sha256: str,
        result: ToolExecutionResult,
        context: ToolExecutionContext,
        prerequisite_decisions: Mapping[str, str],
        capability_decisions: Mapping[str, str],
        model_turn: int,
        session_id: str | None,
        binding_resolution_status: Literal[
            "resolved", "ambiguous", "uncompiled"
        ] = "resolved",
    ) -> None:
        trace_node_id = (
            node.node_id
            if node is not None
            else f"binding_resolution::{binding_resolution_status}"
        )
        trace = make_trace_record(
            sequence=self._next_sequence,
            request_id=context.request_id,
            run_id=context.run_id,
            session_id=session_id or context.run_id,
            stage=context.stage,
            node_id=trace_node_id,
            model_turn=model_turn,
            tool_call_id=call_id,
            model_tool_name=model_tool_name,
            binding=(
                node.binding
                if node is not None
                else _binding_resolution_trace_binding(
                    model_tool_name=model_tool_name,
                    binding_resolution_status=binding_resolution_status,
                )
            ),
            binding_resolution_status=binding_resolution_status,
            input_sha256=input_sha256,
            prerequisite_decisions={
                key: ToolTraceDecision(value)
                for key, value in prerequisite_decisions.items()
            },
            capability_decisions={
                key: ToolTraceDecision(value)
                for key, value in capability_decisions.items()
            },
            result=result,
            summary_max_utf8=(
                self._summary_max_utf8_for_node(node)
                if node is not None
                else MAX_MODEL_SUMMARY_UTF8
            ),
        )
        self._trace_records.append(trace)
        self._next_sequence += 1

    def _summary_max_utf8_for_node(self, node: CompiledHarnessNode) -> int:
        output_policy = self._output_policy_for_node(node)
        if output_policy is None:
            return MAX_MODEL_SUMMARY_UTF8
        return output_policy.max_summary_utf8

    def _output_policy_for_node(
        self, node: CompiledHarnessNode
    ) -> ToolOutputPolicy | None:
        for descriptor in iter_builtin_tool_descriptors():
            if (
                descriptor.tool_id == node.binding.tool_id
                and descriptor.tool_version == node.binding.tool_version
            ):
                return descriptor.output_policy
        return None


def _builtin_pre_entry_policy_result(
    call: ValidatedToolCall,
    context: ToolExecutionContext,
    input_sha256: str,
) -> ToolExecutionResult | None:
    """Lazily consult the built-in pre-entry policy hook.

    The lazy import avoids a module cycle with ``builtin_runtime`` while keeping
    the executor-owned policy gate in the runtime boundary.
    """
    if not call.binding.tool_id.startswith("builtin."):
        return None
    from millforge.tools.builtin_runtime import validate_builtin_pre_entry_policy

    return validate_builtin_pre_entry_policy(
        call,
        context,
        input_sha256=input_sha256,
    )


def create_tool_executor(
    *,
    plan: CompiledHarnessPlan,
    descriptor_snapshot: ToolCatalogSnapshot,
    runtime_registry: RuntimeToolRegistry,
) -> CompiledToolBindingExecutor:
    """Create a compiled-plan scoped executor."""
    return CompiledToolBindingExecutor(
        plan=plan,
        descriptor_snapshot=descriptor_snapshot,
        runtime_registry=runtime_registry,
    )


def _node_binding_defect(
    *,
    node: CompiledHarnessNode,
    descriptor_snapshot: ToolCatalogSnapshot,
    runtime_registry: RuntimeToolRegistry,
) -> tuple[ToolBindingDenialCode, str, dict[str, str], bool] | None:
    lookup = descriptor_snapshot.resolve_exact(
        node.binding.tool_id,
        node.binding.tool_version,
    )
    if lookup.entry is None:
        return (
            ToolBindingDenialCode.NOT_FOUND,
            "compiled binding is absent from descriptor snapshot",
            {
                "binding_field": "tool_id/tool_version",
                "tool_id": node.binding.tool_id,
                "tool_version": str(node.binding.tool_version),
            },
            False,
        )
    entry = lookup.entry
    expected: dict[str, Any] = {
        "descriptor_sha256": entry.descriptor_sha256,
        "implementation_id": entry.implementation_id,
        "model_tool_name": entry.model_tool_name,
        "input_schema": entry.input_schema,
        "required_capabilities": tuple(entry.required_capabilities),
        "produced_artifact_ids": tuple(entry.produced_artifact_ids),
        "side_effect_class": entry.side_effect_class,
        "idempotency": entry.idempotency,
    }
    actual: dict[str, Any] = {
        "descriptor_sha256": node.binding.descriptor_sha256,
        "implementation_id": node.binding.implementation_id,
        "model_tool_name": node.model_tool_name,
        "input_schema": node.input_schema,
        "required_capabilities": tuple(node.required_capabilities),
        "produced_artifact_ids": tuple(node.produced_artifact_ids),
        "side_effect_class": node.side_effect_class,
        "idempotency": node.idempotency,
    }
    for field, expected_value in expected.items():
        if not _projection_values_equal(actual[field], expected_value):
            return (
                ToolBindingDenialCode.BINDING_MISMATCH,
                "compiled node projection does not match descriptor snapshot",
                {
                    "binding_field": field,
                    "expected": _evidence_text(expected_value),
                    "actual": _evidence_text(actual[field]),
                },
                False,
            )
    if runtime_registry.resolve(node.binding.implementation_id) is None:
        return (
            ToolBindingDenialCode.NOT_FOUND,
            "runtime implementation is not registered",
            {
                "binding_field": "implementation_id",
                "implementation_id": node.binding.implementation_id,
            },
            True,
        )
    return None


def _call_binding_mismatch(
    call: ValidatedToolCall,
    node: CompiledHarnessNode,
) -> dict[str, str] | None:
    for field in (
        "tool_id",
        "tool_version",
        "descriptor_sha256",
        "implementation_id",
    ):
        actual = getattr(call.binding, field)
        expected = getattr(node.binding, field)
        if actual != expected:
            return {
                "binding_field": field,
                "expected": _evidence_text(expected),
                "actual": _evidence_text(actual),
            }
    return None


def _denial_result(
    *,
    call_id: str,
    input_sha256: str,
    code: ToolBindingDenialCode,
    summary: str,
    evidence: Mapping[str, str],
    node: CompiledHarnessNode | None = None,
    status: ToolExecutionStatus | None = None,
) -> ToolExecutionResult:
    result_code = ToolExecutionErrorCode(code.value)
    return make_denial_result(
        call_id=call_id,
        code=result_code,
        summary=summary,
        side_effect_class=node.side_effect_class
        if node is not None
        else SideEffectClass.READ_ONLY,
        idempotency=node.idempotency if node is not None else _default_idempotency(),
        input_sha256=input_sha256,
        evidence={key: _evidence_text(value) for key, value in evidence.items()},
        status=status,
    )


def _default_idempotency() -> Any:
    from millforge import IdempotencyClass

    return IdempotencyClass.IDEMPOTENT


def _binding_resolution_trace_binding(
    *,
    model_tool_name: str,
    binding_resolution_status: str,
) -> ToolBindingRef:
    return ToolBindingRef(
        tool_id=model_tool_name,
        tool_version=1,
        descriptor_sha256="0" * 64,
        implementation_id=f"binding-resolution::{binding_resolution_status}",
    )


def _duplicate_values(values: Any) -> frozenset[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return frozenset(duplicates)


def _projection_values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        return canonical_json_serialize(_json_value(left)) == canonical_json_serialize(
            _json_value(right)
        )
    return left == right


def _evidence_text(value: Any) -> str:
    value = _json_value(value)
    text = (
        canonical_json_serialize(value).strip()
        if isinstance(value, Mapping)
        else str(value)
    )
    return text[:512]


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value
