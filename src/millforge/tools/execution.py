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
from millforge.tools.registry import ToolOutputPolicy
from millforge.tools.results import (
    MAX_MODEL_SUMMARY_UTF8,
    ToolExecutionErrorCode,
    canonical_sha256,
    make_denial_result,
    make_tool_result,
    make_trace_record,
    redact_tool_value,
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
        connector_admission_snapshot: Any | None = None,
        connector_broker: Any | None = None,
    ) -> None:
        self._plan = plan
        self._nodes_by_id = {node.node_id: node for node in plan.nodes}
        self._node_by_model_name: dict[str, CompiledHarnessNode] = {}
        self._terminal_result_map = dict(plan.terminal_result_map)
        self._conflicting_model_names = _duplicate_values(
            node.model_tool_name for node in plan.nodes
        )
        for node in plan.nodes:
            if node.model_tool_name not in self._conflicting_model_names:
                self._node_by_model_name[node.model_tool_name] = node
        _validate_connector_admissions(
            plan=plan,
            connector_admission_snapshot=connector_admission_snapshot,
            connector_broker=connector_broker,
        )
        self._node_defects = {
            node.node_id: _node_binding_defect(
                node=node,
                descriptor_snapshot=descriptor_snapshot,
                runtime_registry=runtime_registry,
            )
            for node in plan.nodes
        }
        self._descriptor_snapshot = descriptor_snapshot
        self._connector_admission_snapshot = connector_admission_snapshot
        self._connector_broker = connector_broker
        self._runtime_registry = runtime_registry
        self._trace_records: list[Any] = []
        self._next_sequence = 1

    def fork_for_invocation(self) -> CompiledToolBindingExecutor:
        """Create an executor with identical bindings and fresh trace state."""
        return CompiledToolBindingExecutor(
            plan=self._plan,
            descriptor_snapshot=self._descriptor_snapshot,
            runtime_registry=self._runtime_registry,
            connector_admission_snapshot=self._connector_admission_snapshot,
            connector_broker=self._connector_broker,
        )

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
                connector_audit = (
                    self._connector_pre_entry_audit(
                        node=node,
                        context=context,
                    )
                    if _is_connector_tool_id(node.binding.tool_id)
                    else None
                )
                self._record_trace(
                    node=node,
                    call_id=call_id,
                    model_tool_name=model_tool_name,
                    input_sha256=resolved.input_sha256,
                    result=resolved,
                    context=context,
                    prerequisite_decisions={},
                    capability_decisions={},
                    connector_audit=connector_audit,
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
                    connector_audit=None,
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
                connector_audit=None,
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
                connector_audit=None,
                model_turn=model_turn,
                session_id=session_id,
            )
            return result
        preflight = self._preflight_result(
            node=node,
            call=call,
            context=context,
            prerequisite_results=prerequisite_results or {},
            input_sha256=input_sha256,
        )
        if preflight is not None:
            result, prerequisite_decisions, capability_decisions = preflight
            connector_audit = self._connector_pre_entry_audit(
                node=node,
                context=context,
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
                connector_audit=connector_audit,
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
        if _is_connector_tool_id(node.binding.tool_id):
            result, connector_audit = await self._execute_connector(
                node=node,
                call=call,
                context=context,
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
                connector_audit=connector_audit,
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
            connector_audit=None,
            model_turn=model_turn,
            session_id=session_id,
        )
        return result

    async def _execute_connector(
        self,
        *,
        node: CompiledHarnessNode,
        call: ValidatedToolCall,
        context: ToolExecutionContext,
        input_sha256: str,
    ) -> tuple[ToolExecutionResult, dict[str, Any] | None]:
        from millforge.connectors.broker import (
            ConnectorBrokerOutcome,
            ConnectorInvocationRequest,
            connector_idempotency_key,
        )

        assert self._connector_admission_snapshot is not None
        admission = self._connector_admission_snapshot.require(
            node.binding.tool_id,
            node.binding.tool_version,
            node.binding.descriptor_sha256,
        )
        connector_audit = _connector_approval_audit_fields(
            admission=admission,
            node=node,
            context=context,
            broker_attempted=False,
        )
        approval_decision = connector_audit["approval_decision"]
        if approval_decision == "forbidden":
            return make_denial_result(
                call_id=call.call_id,
                code=ToolExecutionErrorCode.POLICY_DENIED,
                summary="connector approval policy forbids runtime invocation",
                evidence=connector_audit,
                side_effect_class=node.side_effect_class,
                idempotency=node.idempotency,
                input_sha256=input_sha256,
            ), connector_audit
        if approval_decision == "pending":
            return make_denial_result(
                call_id=call.call_id,
                code=ToolExecutionErrorCode.POLICY_DENIED,
                summary="connector approval is pending operator out-of-band review",
                evidence=connector_audit,
                side_effect_class=node.side_effect_class,
                idempotency=node.idempotency,
                input_sha256=input_sha256,
            ), connector_audit
        if approval_decision != "approved":
            return make_denial_result(
                call_id=call.call_id,
                code=ToolExecutionErrorCode.POLICY_DENIED,
                summary="connector approval policy is not satisfied",
                evidence=connector_audit,
                side_effect_class=node.side_effect_class,
                idempotency=node.idempotency,
                input_sha256=input_sha256,
            ), connector_audit
        broker = self._connector_broker
        if broker is None or not broker.has_provider_tool(
            admission.connector_id,
            admission.provider_tool_name,
        ):
            return make_denial_result(
                call_id=call.call_id,
                code=ToolExecutionErrorCode.NOT_FOUND,
                summary="connector provider tool is not available from broker",
                evidence={
                    "connector_id": admission.connector_id,
                    "provider_tool_name": admission.provider_tool_name,
                },
                side_effect_class=node.side_effect_class,
                idempotency=node.idempotency,
                input_sha256=input_sha256,
            ), connector_audit
        drift = _connector_pre_entry_drift(
            admission=admission,
            node=node,
            broker=broker,
        )
        if drift is not None:
            field, expected, actual = drift
            return make_denial_result(
                call_id=call.call_id,
                code=ToolExecutionErrorCode.BINDING_MISMATCH,
                summary="connector provider evidence drifted before broker entry",
                evidence={
                    "connector_id": admission.connector_id,
                    "provider_tool_name": admission.provider_tool_name,
                    "binding_field": field,
                    "expected": expected,
                    "actual": actual,
                },
                side_effect_class=node.side_effect_class,
                idempotency=node.idempotency,
                input_sha256=input_sha256,
            ), {**connector_audit, "drift_decision": "failed"}
        connector_audit = {
            **connector_audit,
            "broker_attempted": True,
            "drift_decision": "passed",
        }
        request = ConnectorInvocationRequest.from_runtime(
            connector_id=admission.connector_id,
            provider_tool_name=admission.provider_tool_name,
            tool_id=node.binding.tool_id,
            tool_version=node.binding.tool_version,
            descriptor_sha256=node.binding.descriptor_sha256,
            connector_identity_sha256=admission.connector_identity_sha256,
            discovery_snapshot_sha256=admission.discovery_snapshot_sha256,
            raw_tool_sha256=admission.raw_tool_sha256,
            arguments=call.arguments,
            request_id=context.request_id,
            run_id=context.run_id,
            stage_plane=context.stage.plane,
            stage_kind_id=context.stage.stage_kind_id,
            stage_node_id=context.stage.node_id,
            timeout_seconds=context.timeout.timeout_seconds,
            deadline_remaining_seconds=context.deadline.remaining(
                lambda: context.current_monotonic
            ),
            cancellation_requested=context.cancellation_requested,
            cancellation_id=context.cancellation.cancellation_id,
            idempotency_key=connector_idempotency_key(
                idempotency=node.idempotency,
                call_id=call.call_id,
            ),
        )
        try:
            pending = broker.invoke(request)
            outcome = await pending if inspect.isawaitable(pending) else pending
        except Exception as exc:
            return make_tool_result(
                call_id=call.call_id,
                status=ToolExecutionStatus.HARD_FAILURE,
                code=ToolExecutionErrorCode.IMPLEMENTATION_ERROR,
                summary="connector broker raised an exception",
                structured_data={
                    "category": ToolExecutionErrorCode.IMPLEMENTATION_ERROR.value,
                    "error_type": type(exc).__name__,
                },
                side_effect_class=node.side_effect_class,
                idempotency=node.idempotency,
                side_effect_certainty=SideEffectCertainty.COMPLETION_UNKNOWN,
                side_effect_record=SideEffectRecord(
                    certainty=SideEffectCertainty.COMPLETION_UNKNOWN,
                    detail_code=ToolExecutionErrorCode.AMBIGUOUS_SIDE_EFFECT.value,
                    summary="broker failure left connector completion unknown",
                    retry_allowed=False,
                ),
                input_sha256=input_sha256,
                retryable=False,
            ), connector_audit
        if not isinstance(outcome, ConnectorBrokerOutcome):
            return make_tool_result(
                call_id=call.call_id,
                status=ToolExecutionStatus.HARD_FAILURE,
                code=ToolExecutionErrorCode.IMPLEMENTATION_ERROR,
                summary="connector broker returned an invalid outcome",
                structured_data={
                    "category": ToolExecutionErrorCode.IMPLEMENTATION_ERROR.value,
                    "error_type": type(outcome).__name__,
                },
                side_effect_class=node.side_effect_class,
                idempotency=node.idempotency,
                side_effect_certainty=SideEffectCertainty.COMPLETION_UNKNOWN,
                input_sha256=input_sha256,
                retryable=False,
            ), connector_audit
        result = _connector_result_from_broker_outcome(
            call_id=call.call_id,
            node=node,
            outcome=outcome,
            input_sha256=input_sha256,
            idempotency_key_policy=admission.idempotency_key_policy,
            idempotency_key=request.idempotency_key,
        )
        return (
            self._validate_implementation_result(
                node=node,
                call=call,
                result=result,
                input_sha256=input_sha256,
            ),
            connector_audit,
        )

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

    def _connector_pre_entry_audit(
        self,
        *,
        node: CompiledHarnessNode,
        context: ToolExecutionContext,
    ) -> dict[str, Any] | None:
        if not _is_connector_tool_id(node.binding.tool_id):
            return None
        assert self._connector_admission_snapshot is not None
        admission = self._connector_admission_snapshot.require(
            node.binding.tool_id,
            node.binding.tool_version,
            node.binding.descriptor_sha256,
        )
        return _connector_approval_audit_fields(
            admission=admission,
            node=node,
            context=context,
            broker_attempted=False,
        )

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
        connector_audit: Mapping[str, Any] | None,
        model_turn: int,
        session_id: str | None,
        binding_resolution_status: Literal[
            "resolved", "ambiguous", "uncompiled"
        ] = "resolved",
    ) -> None:
        if connector_audit is not None:
            connector_audit = _connector_trace_audit_fields(
                audit=connector_audit,
                input_sha256=input_sha256,
                result=result,
            )
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
            connector_audit=connector_audit,
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
        lookup = self._descriptor_snapshot.resolve_exact(
            node.binding.tool_id,
            node.binding.tool_version,
        )
        if lookup.entry is None or lookup.entry.output_policy is None:
            return None
        if not isinstance(lookup.entry.output_policy, ToolOutputPolicy):
            raise ValueError(
                "descriptor snapshot output_policy must be ToolOutputPolicy"
            )
        return lookup.entry.output_policy


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
    connector_admission_snapshot: Any | None = None,
    connector_broker: Any | None = None,
) -> CompiledToolBindingExecutor:
    """Create a compiled-plan scoped executor."""
    return CompiledToolBindingExecutor(
        plan=plan,
        descriptor_snapshot=descriptor_snapshot,
        runtime_registry=runtime_registry,
        connector_admission_snapshot=connector_admission_snapshot,
        connector_broker=connector_broker,
    )


def _validate_connector_admissions(
    *,
    plan: CompiledHarnessPlan,
    connector_admission_snapshot: Any | None,
    connector_broker: Any | None,
) -> None:
    connector_nodes = [
        node for node in plan.nodes if _is_connector_tool_id(node.binding.tool_id)
    ]
    if not connector_nodes:
        return
    if connector_admission_snapshot is None:
        from millforge.connectors.runtime import ConnectorAdmissionSnapshotError

        raise ConnectorAdmissionSnapshotError(
            "compiled connector descriptors require connector admission snapshot"
        )
    if connector_broker is None:
        from millforge.connectors.runtime import ConnectorAdmissionSnapshotError

        raise ConnectorAdmissionSnapshotError(
            "compiled connector descriptors require connector broker"
        )
    for node in connector_nodes:
        admission = connector_admission_snapshot.require(
            node.binding.tool_id,
            node.binding.tool_version,
            node.binding.descriptor_sha256,
        )
        mismatch = _connector_admission_node_mismatch(admission, node)
        if mismatch is not None:
            from millforge.connectors.runtime import ConnectorAdmissionSnapshotError

            raise ConnectorAdmissionSnapshotError(
                "compiled connector descriptor is inconsistent with admission binding: "
                f"{mismatch}"
            )


def _is_connector_tool_id(tool_id: str) -> bool:
    return tool_id.startswith("connector.")


def _connector_pre_entry_drift(
    *,
    admission: Any,
    node: CompiledHarnessNode,
    broker: Any,
) -> tuple[str, str, str] | None:
    admitted_capabilities = tuple(admission.required_capabilities)
    compiled_capabilities = tuple(node.required_capabilities)
    if compiled_capabilities != admitted_capabilities:
        return (
            "required_capabilities",
            _evidence_text(admitted_capabilities),
            _evidence_text(compiled_capabilities),
        )
    evidence_factory = getattr(broker, "provider_tool_evidence", None)
    if not callable(evidence_factory):
        return ("provider_tool_evidence", "present", "missing")
    evidence = evidence_factory(admission.connector_id, admission.provider_tool_name)
    if evidence is None:
        return ("provider_tool_evidence", "present", "missing")
    expected_values = {
        "connector_id": admission.connector_id,
        "provider_tool_name": admission.provider_tool_name,
        "connector_identity_sha256": admission.connector_identity_sha256,
        "discovery_snapshot_sha256": admission.discovery_snapshot_sha256,
        "raw_tool_sha256": admission.raw_tool_sha256,
        "input_schema_sha256": admission.input_schema_sha256,
        "output_schema_sha256": admission.output_schema_sha256,
        "provider_description_sha256": admission.provider_description_sha256,
    }
    for field, expected in expected_values.items():
        if expected is None:
            continue
        actual = getattr(evidence, field, None)
        if actual != expected:
            return (field, _evidence_text(expected), _evidence_text(actual))
    return None


def _connector_admission_node_mismatch(
    admission: Any, node: CompiledHarnessNode
) -> str | None:
    expected = {
        "side_effect_class": admission.side_effect_class,
        "idempotency": admission.idempotency,
    }
    actual = {
        "side_effect_class": node.side_effect_class,
        "idempotency": node.idempotency,
    }
    for field, expected_value in expected.items():
        if not _projection_values_equal(actual[field], expected_value):
            return field
    return None


def _connector_approval_identity_kind(grant: Any | None) -> str:
    if grant is None:
        return "none"
    has_approval_id = getattr(grant, "approval_id", None) is not None
    has_nonce = getattr(grant, "nonce", None) is not None
    if has_approval_id and has_nonce:
        return "approval_id_and_nonce"
    if has_approval_id:
        return "approval_id"
    if has_nonce:
        return "nonce"
    return "none"


def _connector_approval_evidence(
    grants: tuple[Any, ...],
    representative: Any | None = None,
) -> dict[str, Any]:
    representative_grant = (
        representative
        if representative is not None
        else (grants[0] if grants else None)
    )
    return {
        "grant_count": len(grants),
        "approval_id_present": any(
            getattr(grant, "approval_id", None) is not None for grant in grants
        ),
        "nonce_present": any(
            getattr(grant, "nonce", None) is not None for grant in grants
        ),
        "identity_kind": _connector_approval_identity_kind(representative_grant),
    }


def _connector_explicit_approval_assessment(
    *,
    admission: Any,
    node: CompiledHarnessNode,
    context: ToolExecutionContext,
) -> tuple[str, dict[str, Any]]:
    grants = context.connector_approval_grants
    if not grants:
        return "missing", _connector_approval_evidence(grants)
    expired_scope_seen = False
    wrong_stage_seen = False
    wrong_run_seen = False
    wrong_scope_seen = False
    for grant in grants:
        scope_matches = (
            grant.connector_id == admission.connector_id
            and grant.provider_tool_name == admission.provider_tool_name
            and grant.tool_id == node.binding.tool_id
            and grant.tool_version == node.binding.tool_version
            and grant.descriptor_sha256 == node.binding.descriptor_sha256
            and grant.request_id == context.request_id
            and grant.run_id == context.run_id
            and _stage_identity_matches(grant.stage, context.stage)
            and grant.approval_policy == admission.approval_policy.value
        )
        if scope_matches:
            if grant.expires_at_monotonic <= context.current_monotonic:
                expired_scope_seen = True
                continue
            return "approved", _connector_approval_evidence(grants, grant)
        wrong_stage_seen = wrong_stage_seen or (
            grant.connector_id == admission.connector_id
            and grant.provider_tool_name == admission.provider_tool_name
            and grant.tool_id == node.binding.tool_id
            and grant.tool_version == node.binding.tool_version
            and grant.descriptor_sha256 == node.binding.descriptor_sha256
            and grant.request_id == context.request_id
            and grant.run_id == context.run_id
            and not _stage_identity_matches(grant.stage, context.stage)
        )
        wrong_run_seen = wrong_run_seen or (
            grant.connector_id == admission.connector_id
            and grant.provider_tool_name == admission.provider_tool_name
            and grant.tool_id == node.binding.tool_id
            and grant.tool_version == node.binding.tool_version
            and grant.descriptor_sha256 == node.binding.descriptor_sha256
            and grant.request_id == context.request_id
            and grant.run_id != context.run_id
        )
        wrong_scope_seen = wrong_scope_seen or (
            grant.approval_policy == admission.approval_policy.value
        )
    if expired_scope_seen:
        return "expired_or_stale", _connector_approval_evidence(grants)
    if wrong_stage_seen:
        return "wrong_stage", _connector_approval_evidence(grants)
    if wrong_run_seen:
        return "wrong_run", _connector_approval_evidence(grants)
    if wrong_scope_seen:
        return "wrong_scope", _connector_approval_evidence(grants)
    return "missing", _connector_approval_evidence(grants)


def _connector_approval_audit_fields(
    *,
    admission: Any,
    node: CompiledHarnessNode,
    context: ToolExecutionContext,
    broker_attempted: bool,
) -> dict[str, Any]:
    from millforge.connectors import ConnectorApprovalPolicy

    base = {
        "connector_id": admission.connector_id,
        "provider_tool_name": admission.provider_tool_name,
        "connector_tool_id": node.binding.tool_id,
        "connector_tool_version": node.binding.tool_version,
        "connector_descriptor_sha256": node.binding.descriptor_sha256,
        "connector_identity_sha256": admission.connector_identity_sha256,
        "discovery_snapshot_sha256": admission.discovery_snapshot_sha256,
        "approval_policy": admission.approval_policy.value,
        "broker_attempted": broker_attempted,
        "drift_decision": "not_reached",
    }
    if admission.approval_policy is ConnectorApprovalPolicy.NONE:
        decision = "approved"
        evidence = _connector_approval_evidence(context.connector_approval_grants)
    elif admission.approval_policy is ConnectorApprovalPolicy.FORBIDDEN:
        decision = "forbidden"
        evidence = _connector_approval_evidence(context.connector_approval_grants)
    elif admission.approval_policy is ConnectorApprovalPolicy.OPERATOR_OUT_OF_BAND:
        decision = "pending"
        evidence = _connector_approval_evidence(context.connector_approval_grants)
    elif admission.approval_policy is ConnectorApprovalPolicy.MILLRACE_EXPLICIT:
        decision, evidence = _connector_explicit_approval_assessment(
            admission=admission,
            node=node,
            context=context,
        )
    else:
        decision = "missing"
        evidence = _connector_approval_evidence(context.connector_approval_grants)
    return {
        **base,
        "approval_decision": decision,
        "approval_evidence": evidence,
    }


def _connector_trace_audit_fields(
    *,
    audit: Mapping[str, Any],
    input_sha256: str,
    result: ToolExecutionResult,
) -> dict[str, Any]:
    result_evidence = {}
    if isinstance(result.structured_data, Mapping):
        raw_evidence = result.structured_data.get("evidence")
        if isinstance(raw_evidence, Mapping):
            result_evidence = {
                key: raw_evidence[key]
                for key in ("binding_field", "expected", "actual")
                if key in raw_evidence
            }
    redacted_evidence = redact_tool_value(
        {
            "approval_decision": audit.get("approval_decision"),
            "approval_evidence": audit.get("approval_evidence", {}),
            "broker_attempted": audit.get("broker_attempted"),
            "drift_decision": audit.get("drift_decision"),
            **result_evidence,
            "error_code": result.error_code,
            "execution_status": result.status.value,
            "side_effect_certainty": result.side_effect_certainty.value,
            "summary": result.summary,
        }
    )
    return {
        **audit,
        "request_sha256": input_sha256,
        "response_sha256": canonical_sha256(redact_tool_value(result.structured_data)),
        "retry_decision": "retry_allowed" if result.retryable else "retry_denied",
        "redacted_evidence": redacted_evidence,
    }


def _connector_result_from_broker_outcome(
    *,
    call_id: str,
    node: CompiledHarnessNode,
    outcome: Any,
    input_sha256: str,
    idempotency_key_policy: str | None,
    idempotency_key: str | None,
) -> ToolExecutionResult:
    status = outcome.status
    code = _connector_error_code_for_outcome(outcome)
    certainty = outcome.side_effect_certainty
    retryable = _connector_retryable_for_outcome(
        status=status,
        certainty=certainty,
        idempotency=node.idempotency,
        requested_retryable=outcome.retryable,
        idempotency_key_policy=idempotency_key_policy,
        idempotency_key=idempotency_key,
    )
    side_effect_record = _connector_side_effect_record(
        status=status,
        code=code,
        certainty=certainty,
        summary=outcome.summary,
        retryable=retryable,
    )
    return make_tool_result(
        call_id=call_id,
        status=status,
        code=code,
        summary=outcome.summary,
        structured_data=outcome.structured_data,
        side_effect_class=node.side_effect_class,
        idempotency=node.idempotency,
        side_effect_certainty=certainty,
        side_effect_record=side_effect_record,
        input_sha256=input_sha256,
        retryable=retryable,
    )


def _connector_error_code_for_outcome(outcome: Any) -> ToolExecutionErrorCode | None:
    if outcome.status is ToolExecutionStatus.SUCCESS:
        return None
    if outcome.error_code is not None:
        return outcome.error_code
    if outcome.status is ToolExecutionStatus.TIMED_OUT:
        return ToolExecutionErrorCode.TIMEOUT
    if outcome.status is ToolExecutionStatus.CANCELLED:
        return ToolExecutionErrorCode.CANCELLED
    if outcome.status is ToolExecutionStatus.AMBIGUOUS:
        return ToolExecutionErrorCode.AMBIGUOUS_SIDE_EFFECT
    return ToolExecutionErrorCode.IMPLEMENTATION_ERROR


def _connector_retryable_for_outcome(
    *,
    status: ToolExecutionStatus,
    certainty: SideEffectCertainty,
    idempotency: Any,
    requested_retryable: bool,
    idempotency_key_policy: str | None,
    idempotency_key: str | None,
) -> bool:
    if status is ToolExecutionStatus.SUCCESS:
        return False
    if not requested_retryable:
        return False
    from millforge import IdempotencyClass

    has_safe_idempotency_key = (
        idempotency is IdempotencyClass.IDEMPOTENT_WITH_KEY
        and idempotency_key_policy == "call_id"
        and idempotency_key is not None
    )
    if certainty is SideEffectCertainty.CONFIRMED_COMPLETE:
        return has_safe_idempotency_key
    if idempotency is IdempotencyClass.IDEMPOTENT:
        return True
    return has_safe_idempotency_key


def _connector_side_effect_record(
    *,
    status: ToolExecutionStatus,
    code: ToolExecutionErrorCode | None,
    certainty: SideEffectCertainty,
    summary: str,
    retryable: bool,
) -> SideEffectRecord | None:
    if status is ToolExecutionStatus.SUCCESS:
        return None
    detail_code = (
        code.value
        if code is not None
        else ToolExecutionErrorCode.IMPLEMENTATION_ERROR.value
    )
    return SideEffectRecord(
        certainty=certainty,
        detail_code=detail_code,
        summary=f"connector broker outcome: {summary}",
        retry_allowed=retryable,
    )


def _connector_explicit_approval_denial(
    *,
    admission: Any,
    node: CompiledHarnessNode,
    context: ToolExecutionContext,
) -> dict[str, Any] | None:
    audit = _connector_approval_audit_fields(
        admission=admission,
        node=node,
        context=context,
        broker_attempted=False,
    )
    if audit["approval_decision"] == "approved":
        return None
    return audit


def _stage_identity_matches(left: Any, right: Any) -> bool:
    return (
        getattr(left, "plane", None) == getattr(right, "plane", None)
        and getattr(left, "stage_kind_id", None)
        == getattr(right, "stage_kind_id", None)
        and getattr(left, "node_id", None) == getattr(right, "node_id", None)
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
    if entry.output_policy is not None and not isinstance(
        entry.output_policy, ToolOutputPolicy
    ):
        return (
            ToolBindingDenialCode.BINDING_MISMATCH,
            "descriptor snapshot output policy is malformed",
            {"binding_field": "output_policy"},
            True,
        )
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
    connector_tool = _is_connector_tool_id(node.binding.tool_id)
    for field, expected_value in expected.items():
        if connector_tool and field == "required_capabilities":
            continue
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
    if connector_tool:
        return None
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
