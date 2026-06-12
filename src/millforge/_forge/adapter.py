"""Private translation from compiled Millforge plans to Forge objects."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from millforge._forge.clients.base import StreamChunk, TokenUsage as ForgeTokenUsage
from millforge._forge.context.manager import ContextManager
from millforge._forge.context.strategies import TieredCompact
from millforge._forge.core.messages import (
    Message,
    MessageMeta,
    MessageRole,
    MessageType,
)
from millforge._forge.core.runner import WorkflowRunner
from millforge._forge.core.workflow import (
    TextResponse,
    ToolCall,
    ToolDef,
    ToolSpec,
    Workflow,
)
from millforge._forge.errors import (
    ContextBudgetExceeded,
    ForgeError,
    MaxIterationsError,
    NonRetryableToolError,
    PrerequisiteError,
    StepEnforcementError,
    ToolCallError,
    ToolExecutionError,
    ToolResolutionError,
    WorkflowCancelledError,
)
from millforge.compiled_plan import (
    ArgumentMatch,
    CompiledContextPolicy,
    CompiledHarnessNode,
    CompiledHarnessPlan,
    CompiledPrerequisite,
    DiagnosticField,
    IdempotencyClass,
    SessionEvent,
    SessionEventType,
    SideEffectCertainty,
    SideEffectClass,
    ToolExecutionStatus,
    ToolTraceDecision,
    ToolTraceDecisionRecord,
    ToolTraceIdempotency,
    ToolTraceRecord,
    ToolTraceSideEffectClass,
    canonical_json_serialize,
)
from millforge.contracts import (
    ArtifactRef,
    AssistantMessage,
    CancellationRef,
    Deadline,
    DiagnosticMetadata,
    GuardedSessionResult,
    GuardedSessionStatus,
    GuardedSessionRequest,
    HarnessExecutionRequest,
    InvalidToolArguments,
    ModelCompletionRequest,
    ModelCompletionResponse,
    ModelMessage,
    ModelToolDefinition,
    ModelToolCall,
    ParsedToolArguments,
    SamplingRequest,
    SystemMessage,
    TerminalIntent,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolResultMessage,
    TimingMetadata,
    TokenUsage,
    UserMessage,
    UsageMetadata,
    ValidatedToolCall,
)
from millforge.exceptions import (
    BackendTranslationError,
    DeadlineExceededError,
    ModelTransportError,
    OperationCancelledError,
    ToolInvokeError,
)
from millforge.protocols import (
    CancellationResolver,
    CompiledHarnessLoader,
    ModelClient,
    RuntimeClock,
    ToolExecutor,
)


class ModelClientLike(Protocol):
    async def complete(
        self, request: ModelCompletionRequest
    ) -> ModelCompletionResponse: ...


class ToolExecutorLike(Protocol):
    async def execute(
        self, call: ValidatedToolCall, context: ToolExecutionContext
    ) -> ToolExecutionResult: ...

    def supports_tool(self, name: str) -> bool: ...


class CancellationTokenLike(Protocol):
    def is_cancelled(self) -> bool: ...

    @property
    def reason(self) -> str | None: ...


class CancellationResolverLike(Protocol):
    def resolve(self, ref: Any) -> CancellationTokenLike: ...


class RuntimeClockLike(Protocol):
    def utc_now(self) -> Any: ...

    def monotonic(self) -> float: ...


class ForgeBindingRejectedError(ValueError):
    """Compiled semantics cannot be represented by the private Forge subset."""

    code = "binding_rejected"


class ForgeBridgeError(ValueError):
    """Bridge-owned validation rejected a private Forge interaction."""

    code = "bridge_rejected"


DiagnosticCategory = Literal[
    "binding",
    "compiled_harness",
    "backend",
    "model",
    "tool",
    "budget",
    "timeout",
    "cancellation",
    "artifact",
    "internal",
]


@dataclass(frozen=True)
class TerminalCandidate:
    """Internal terminal candidate before it is accepted as ``TerminalIntent``."""

    call_id: str
    node_id: str
    tool_name: str
    terminal_result: str
    summary: str
    artifact_refs: tuple[ArtifactRef, ...]


@dataclass(frozen=True)
class ForgeRunnerOptions:
    """Budget values passed to ``WorkflowRunner``."""

    max_iterations: int
    max_retries_per_step: int
    max_tool_errors: int
    max_premature_attempts: int
    max_prereq_violations: int


@dataclass(frozen=True)
class ForgeWorkflowInput:
    """Private Forge workflow plus Millforge-owned translation metadata."""

    workflow: Workflow
    runner_options: ForgeRunnerOptions
    binding_by_tool: dict[str, str]
    node_id_by_tool: dict[str, str]
    terminal_result_by_tool: dict[str, str]
    cancellation_id: str | None = None


class ForgeGuardrailBackend:
    """GuardrailBackend implementation backed by the private Forge runner subset."""

    def __init__(
        self,
        *,
        model_client: ModelClient,
        tool_executor: ToolExecutor,
        plan_loader: CompiledHarnessLoader,
        context_factory: ForgeContextFactory,
        clock: RuntimeClock,
        cancellation_resolver: CancellationResolver,
    ) -> None:
        self._model_client = model_client
        self._tool_executor = tool_executor
        self._plan_loader = plan_loader
        self._context_factory = context_factory
        self._clock = clock
        self._cancellation_resolver = cancellation_resolver
        self._requests: list[GuardedSessionRequest] = []

    @property
    def requests(self) -> tuple[GuardedSessionRequest, ...]:
        return tuple(self._requests)

    @property
    def call_count(self) -> int:
        return len(self._requests)

    async def run_session(self, request: GuardedSessionRequest) -> GuardedSessionResult:
        """Run one guarded Forge workflow session."""
        self._requests.append(request)
        started_at = self._clock.utc_now()
        event_translator = ForgeEventTranslator(
            session_request=request,
            clock=self._clock,
        )
        tool_bridge: ForgeToolBridge | None = None
        model_bridge: ForgeModelBridge | None = None

        try:
            self._validate_request(request)
            token = self._cancellation_resolver.resolve(
                request.execution_request.cancellation
            )
            self._check_deadline(request)
            self._check_cancelled(token)
            self._check_remaining_deadline(request)

            plan = await self._load_verified_plan(request)
            self._recheck_plan_against_request(plan, request)
            event_translator.emit(SessionEventType.SESSION_STARTED)

            model_bridge = ForgeModelBridge(
                model_client=self._model_client,
                model=request.execution_request.model_profile.profile_id,
                event_translator=event_translator,
            )
            tool_bridge = ForgeToolBridge(
                plan=plan,
                session_request=request,
                executor=self._tool_executor,
                cancellation_resolver=self._cancellation_resolver,
                clock=self._clock,
                call_id_resolver=model_bridge.resolve_tool_call_id,
            )
            workflow_input = ForgeWorkflowFactory(
                {
                    node.binding.implementation_id: tool_bridge.make_callable(
                        node.model_tool_name
                    )
                    for node in plan.nodes
                },
                cancellation_id=token.cancellation_id,
            ).build(plan)
            event_translator.workflow_constructed(
                tool_count=len(workflow_input.workflow.tools)
            )
            context_manager = self._context_factory.build(plan.context_policy)
            runner = WorkflowRunner(
                model_bridge,
                context_manager,
                max_iterations=workflow_input.runner_options.max_iterations,
                max_retries_per_step=workflow_input.runner_options.max_retries_per_step,
                max_tool_errors=workflow_input.runner_options.max_tool_errors,
                max_premature_attempts=workflow_input.runner_options.max_premature_attempts,
                max_prereq_violations=workflow_input.runner_options.max_prereq_violations,
                stream=False,
            )
            initial_messages = ForgeSessionInputBuilder().build(plan, request)
            cancel_event = cancellation_event_from_ref(token.is_cancelled())
            await runner.run(
                workflow_input.workflow,
                user_message="",
                initial_messages=initial_messages,
                cancel_event=cancel_event,
            )
            if tool_bridge.terminal_intent is None:
                raise ForgeBridgeError("Workflow completed without terminal intent")
            return self._result(
                request,
                status=GuardedSessionStatus.TERMINAL,
                started_at=started_at,
                terminal_intent=tool_bridge.terminal_intent,
                artifact_refs=tool_bridge.terminal_intent.artifact_refs,
                usage=_usage_from_bridges(model_bridge, tool_bridge),
                events=_ordered_events(event_translator.events, tool_bridge.events),
                tool_trace=tool_bridge.tool_trace,
            )
        except OperationCancelledError:
            return self._failure_result(
                request,
                status=GuardedSessionStatus.CANCELLED,
                code="workflow_cancelled",
                category="cancellation",
                message="Workflow cancelled",
                started_at=started_at,
                events=_ordered_events(
                    event_translator.events,
                    tool_bridge.events if tool_bridge is not None else (),
                    _single_event(event_translator, SessionEventType.CANCELLED),
                ),
                tool_trace=tool_bridge.tool_trace if tool_bridge is not None else (),
            )
        except DeadlineExceededError:
            return self._failure_result(
                request,
                status=GuardedSessionStatus.TIMED_OUT,
                code="deadline_expired",
                category="timeout",
                message="Workflow deadline expired",
                started_at=started_at,
                events=_ordered_events(
                    event_translator.events,
                    tool_bridge.events if tool_bridge is not None else (),
                    _single_event(event_translator, SessionEventType.TIMED_OUT),
                ),
                tool_trace=tool_bridge.tool_trace if tool_bridge is not None else (),
            )
        except (ForgeBindingRejectedError, ForgeBridgeError) as exc:
            return self._failure_result(
                request,
                status=GuardedSessionStatus.BACKEND_FAILED,
                code=getattr(exc, "code", "bridge_rejected"),
                category="backend",
                message="Forge backend rejected the compiled workflow",
                started_at=started_at,
                events=_ordered_events(event_translator.events),
            )
        except ToolCallError:
            return self._failure_result(
                request,
                status=GuardedSessionStatus.REJECTED,
                code="malformed_tool_call",
                category="backend",
                message="Model did not produce a valid tool call",
                started_at=started_at,
                events=_ordered_events(event_translator.events),
            )
        except (StepEnforcementError, PrerequisiteError):
            return self._failure_result(
                request,
                status=GuardedSessionStatus.INVALID_TERMINAL,
                code="workflow_order_rejected",
                category="backend",
                message="Model exhausted ordered workflow corrections",
                started_at=started_at,
                events=_ordered_events(
                    event_translator.events,
                    tool_bridge.events if tool_bridge is not None else (),
                ),
                tool_trace=tool_bridge.tool_trace if tool_bridge is not None else (),
            )
        except (MaxIterationsError, ContextBudgetExceeded):
            return self._failure_result(
                request,
                status=GuardedSessionStatus.BUDGET_EXHAUSTED,
                code="workflow_budget_exhausted",
                category="backend",
                message="Workflow budget exhausted",
                started_at=started_at,
                events=_ordered_events(
                    event_translator.events,
                    tool_bridge.events if tool_bridge is not None else (),
                    _single_event(
                        event_translator,
                        SessionEventType.BUDGET_EXHAUSTED,
                        code="workflow_budget_exhausted",
                    ),
                ),
                tool_trace=tool_bridge.tool_trace if tool_bridge is not None else (),
            )
        except WorkflowCancelledError:
            return self._failure_result(
                request,
                status=GuardedSessionStatus.CANCELLED,
                code="workflow_cancelled",
                category="cancellation",
                message="Workflow cancelled",
                started_at=started_at,
                events=_ordered_events(
                    event_translator.events,
                    tool_bridge.events if tool_bridge is not None else (),
                    _single_event(event_translator, SessionEventType.CANCELLED),
                ),
                tool_trace=tool_bridge.tool_trace if tool_bridge is not None else (),
            )
        except (ToolExecutionError, ToolResolutionError, NonRetryableToolError):
            return self._failure_result(
                request,
                status=GuardedSessionStatus.TOOL_FAILED,
                code="tool_execution_failed",
                category="tool",
                message="Tool execution failed",
                started_at=started_at,
                events=_ordered_events(
                    event_translator.events,
                    tool_bridge.events if tool_bridge is not None else (),
                ),
                tool_trace=tool_bridge.tool_trace if tool_bridge is not None else (),
            )
        except (BackendTranslationError, ToolInvokeError):
            return self._failure_result(
                request,
                status=GuardedSessionStatus.BACKEND_FAILED,
                code="backend_translation_failed",
                category="backend",
                message="Forge backend translation failed",
                started_at=started_at,
                events=_ordered_events(event_translator.events),
            )
        except ModelTransportError:
            return self._failure_result(
                request,
                status=GuardedSessionStatus.MODEL_FAILED,
                code="model_transport_failed",
                category="model",
                message="Model transport failed",
                started_at=started_at,
                events=_ordered_events(event_translator.events),
            )
        except ForgeError:
            return self._failure_result(
                request,
                status=GuardedSessionStatus.BACKEND_FAILED,
                code="forge_backend_failed",
                category="backend",
                message="Forge backend failed",
                started_at=started_at,
                events=_ordered_events(event_translator.events),
            )
        except Exception as exc:
            if model_bridge is not None and (
                model_bridge.in_model_call or model_bridge.model_exception_seen
            ):
                status = GuardedSessionStatus.MODEL_FAILED
                code = "model_transport_failed"
                category = "model"
                message = "Model transport failed"
            else:
                status = GuardedSessionStatus.BACKEND_FAILED
                code = "unknown_forge_exception"
                category = "backend"
                message = "Forge backend failed"
            return self._failure_result(
                request,
                status=status,
                code=code,
                category=category,
                message=message,
                started_at=started_at,
                events=_ordered_events(
                    event_translator.events,
                    tool_bridge.events if tool_bridge is not None else (),
                ),
                tool_trace=tool_bridge.tool_trace if tool_bridge is not None else (),
                cause=exc,
            )

    def _validate_request(self, request: GuardedSessionRequest) -> None:
        if not request.session_id.strip():
            raise BackendTranslationError("guarded session_id must be non-empty")
        execution = request.execution_request
        if not execution.request_id.strip() or not execution.run_id.strip():
            raise BackendTranslationError("execution identity must be non-empty")

    def _check_deadline(self, request: GuardedSessionRequest) -> None:
        now = self._clock.monotonic()
        if now >= request.deadline.effective_deadline_monotonic:
            raise DeadlineExceededError("guarded session deadline expired")

    def _check_remaining_deadline(self, request: GuardedSessionRequest) -> None:
        if request.deadline.remaining(self._clock) <= 0:
            raise DeadlineExceededError("guarded session has no remaining deadline")

    def _check_cancelled(self, token: CancellationTokenLike) -> None:
        if token.is_cancelled():
            raise OperationCancelledError(token.reason or "workflow cancelled")

    async def _load_verified_plan(
        self, request: GuardedSessionRequest
    ) -> CompiledHarnessPlan:
        execution = request.execution_request
        plan = await self._plan_loader.load(execution.compiled_harness)
        body = plan.model_dump(mode="json")
        body.pop("compiled_sha256", None)
        computed_hash = hashlib.sha256(
            canonical_json_serialize(body).encode("utf-8")
        ).hexdigest()
        if computed_hash != plan.compiled_sha256:
            raise ForgeBindingRejectedError("Compiled plan canonical hash mismatch")
        if computed_hash != execution.compiled_harness.expected_hash.digest:
            raise ForgeBindingRejectedError("Compiled plan expected hash mismatch")
        return plan

    def _recheck_plan_against_request(
        self,
        plan: CompiledHarnessPlan,
        request: GuardedSessionRequest,
    ) -> None:
        execution = request.execution_request
        ref = execution.compiled_harness
        if plan.harness_id != ref.identity.harness_id:
            raise ForgeBindingRejectedError("Compiled plan harness_id mismatch")
        if plan.harness_version != ref.identity.harness_version:
            raise ForgeBindingRejectedError("Compiled plan harness_version mismatch")
        if execution.stage.plane != "execution":
            raise ForgeBindingRejectedError("Guarded session stage is not execution")
        if execution.stage.stage_kind_id not in plan.stage_kind_ids:
            raise ForgeBindingRejectedError("Compiled plan does not support stage kind")
        if execution.model_profile.profile_id != plan.model_profile.profile_id:
            raise ForgeBindingRejectedError("Compiled plan model profile mismatch")
        granted = {
            grant.capability_id for grant in execution.capability_envelope.grants
        }
        missing = sorted(set(plan.required_capabilities) - granted)
        if missing:
            raise ForgeBindingRejectedError("Compiled plan capability grants missing")

    def _result(
        self,
        request: GuardedSessionRequest,
        *,
        status: GuardedSessionStatus,
        started_at: Any,
        terminal_intent: TerminalIntent | None = None,
        artifact_refs: tuple[ArtifactRef, ...] = (),
        usage: UsageMetadata | None = None,
        diagnostic: DiagnosticMetadata | None = None,
        events: tuple[SessionEvent, ...] = (),
        tool_trace: tuple[ToolTraceRecord, ...] = (),
    ) -> GuardedSessionResult:
        return GuardedSessionResult(
            session_id=request.session_id,
            status=status,
            terminal_intent=terminal_intent,
            artifact_refs=artifact_refs,
            usage=usage,
            timing=_timing_metadata(started_at, self._clock.utc_now()),
            diagnostic=diagnostic,
            events=events,
            tool_trace=tool_trace,
        )

    def _failure_result(
        self,
        request: GuardedSessionRequest,
        *,
        status: GuardedSessionStatus,
        code: str,
        category: DiagnosticCategory,
        message: str,
        started_at: Any,
        events: tuple[SessionEvent, ...],
        tool_trace: tuple[ToolTraceRecord, ...] = (),
        cause: Exception | None = None,
    ) -> GuardedSessionResult:
        _ = cause
        diagnostic = DiagnosticMetadata(
            error_code=code,
            category=category,
            message=message,
            retryable=status
            in {
                GuardedSessionStatus.BACKEND_FAILED,
                GuardedSessionStatus.MODEL_FAILED,
                GuardedSessionStatus.TOOL_FAILED,
            },
            origin=code,
            fields=(),
        )
        return self._result(
            request,
            status=status,
            started_at=started_at,
            diagnostic=diagnostic,
            events=events,
            tool_trace=tool_trace,
        )


class ForgeEventTranslator:
    """Bridge-owned typed event collector for private Forge activity."""

    def __init__(
        self,
        *,
        session_request: GuardedSessionRequest,
        clock: RuntimeClockLike,
    ) -> None:
        self._session_request = session_request
        self._clock = clock
        self._events: list[SessionEvent] = []

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        return tuple(self._events)

    def workflow_constructed(self, *, tool_count: int) -> SessionEvent:
        return self.emit(
            SessionEventType.WORKFLOW_CONSTRUCTED,
            fields={"tool_count": tool_count},
        )

    def correction_issued(self, *, code: str) -> SessionEvent:
        return self.emit(SessionEventType.CORRECTION_ISSUED, code=code)

    def premature_terminal_rejected(
        self, *, node_id: str | None = None
    ) -> SessionEvent:
        return self.emit(
            SessionEventType.PREMATURE_TERMINAL_REJECTED,
            node_id=node_id,
        )

    def context_compacted(self, *, kept_messages: int) -> SessionEvent:
        return self.emit(
            SessionEventType.CONTEXT_COMPACTED,
            fields={"kept_messages": kept_messages},
        )

    def budget_exhausted(self, *, code: str) -> SessionEvent:
        return self.emit(SessionEventType.BUDGET_EXHAUSTED, code=code)

    def sanitized_metadata(
        self, **fields: str | int | float | bool | None
    ) -> tuple[DiagnosticField, ...]:
        return _diagnostic_fields(fields)

    def emit(
        self,
        event_type: SessionEventType,
        *,
        node_id: str | None = None,
        model_turn: int | None = None,
        tool_call_id: str | None = None,
        code: str | None = None,
        fields: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> SessionEvent:
        request = self._session_request.execution_request
        event = SessionEvent(
            schema_version="1.0",
            sequence=len(self._events) + 1,
            occurred_at=self._clock.utc_now().isoformat(),
            monotonic_offset_ms=self._clock.monotonic() * 1000,
            event_type=event_type,
            request_id=request.request_id,
            run_id=request.run_id,
            session_id=self._session_request.session_id,
            stage=request.stage,
            node_id=node_id,
            model_turn=model_turn,
            tool_call_id=tool_call_id,
            code=code,
            fields=_diagnostic_fields(fields or {}),
        )
        self._events.append(event)
        return event


class ForgeWorkflowFactory:
    """Translate a compiled harness plan into private Forge workflow objects."""

    def __init__(
        self,
        bindings: Mapping[str, Callable[..., Any]],
        *,
        cancellation_id: str | None = None,
    ) -> None:
        self._bindings = dict(bindings)
        self._cancellation_id = cancellation_id

    def build(self, plan: CompiledHarnessPlan) -> ForgeWorkflowInput:
        _validate_plan_identities(plan)
        tool_name_by_node_id = {
            node.node_id: node.model_tool_name for node in plan.nodes
        }
        tools: dict[str, ToolDef] = {}
        binding_by_tool: dict[str, str] = {}
        node_id_by_tool: dict[str, str] = {}
        terminal_result_by_tool: dict[str, str] = {}

        for node in plan.nodes:
            implementation = self._bindings.get(node.binding.implementation_id)
            if implementation is None:
                _reject(
                    "Missing tool binding implementation "
                    f"{node.binding.implementation_id!r} for node {node.node_id!r}"
                )
            spec = _tool_spec_from_node(node)
            tools[node.model_tool_name] = ToolDef(
                spec=spec,
                callable=implementation,
                prerequisites=[
                    _translate_prerequisite(prereq, tool_name_by_node_id)
                    for prereq in node.prerequisites
                ],
            )
            binding_by_tool[node.model_tool_name] = node.binding.implementation_id
            node_id_by_tool[node.model_tool_name] = node.node_id
            if node.terminal_result is not None:
                terminal_result_by_tool[node.model_tool_name] = node.terminal_result

        terminal_tools = [
            node.model_tool_name
            for node in plan.nodes
            if node.terminal_result is not None
        ]
        if not terminal_tools:
            _reject("Compiled plan has no terminal nodes")

        workflow = Workflow(
            name=plan.harness_id,
            description=f"Compiled Millforge harness {plan.harness_id}",
            tools=tools,
            required_steps=[
                node.model_tool_name
                for node in plan.nodes
                if node.required and node.terminal_result is None
            ],
            terminal_tool=terminal_tools,
            system_prompt_template=plan.prompt_policy.system_instructions,
        )
        return ForgeWorkflowInput(
            workflow=workflow,
            runner_options=ForgeRunnerOptions(
                max_iterations=plan.budgets.max_iterations,
                max_retries_per_step=plan.budgets.max_validation_retries,
                max_tool_errors=plan.budgets.max_tool_errors,
                max_premature_attempts=plan.budgets.max_premature_terminal_attempts,
                max_prereq_violations=plan.budgets.max_prerequisite_violations,
            ),
            binding_by_tool=binding_by_tool,
            node_id_by_tool=node_id_by_tool,
            terminal_result_by_tool=terminal_result_by_tool,
            cancellation_id=self._cancellation_id,
        )


class ForgeSessionInputBuilder:
    """Build deterministic private Forge initial messages."""

    def build(
        self,
        plan: CompiledHarnessPlan,
        session_request: GuardedSessionRequest,
    ) -> list[Message]:
        request = session_request.execution_request
        payload = _session_payload(plan, session_request, request)
        content = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return [
            Message(
                MessageRole.SYSTEM,
                plan.prompt_policy.system_instructions,
                MessageMeta(MessageType.SYSTEM_PROMPT),
            ),
            Message(
                MessageRole.USER,
                content,
                MessageMeta(MessageType.USER_INPUT),
            ),
        ]


class ForgeContextFactory:
    """Construct private Forge context management from compiled policy only."""

    def build(self, policy: CompiledContextPolicy) -> ContextManager:
        if policy.strategy_id != "forge.tiered.v1":
            _reject(f"Unsupported context strategy {policy.strategy_id!r}")
        return ContextManager(
            strategy=TieredCompact(
                keep_recent=policy.keep_recent_iterations,
                phase_thresholds=policy.phase_thresholds,
            ),
            budget_tokens=policy.budget_tokens,
            on_compact=_context_compaction_callback,
            context_thresholds=None,
            on_context_threshold=None,
        )


class ForgeModelBridge:
    """Private Forge ``LLMClient`` backed by a public ``ModelClient``."""

    api_format = "openai"

    def __init__(
        self,
        *,
        model_client: ModelClientLike,
        model: str,
        event_translator: ForgeEventTranslator | None = None,
    ) -> None:
        self._model_client = model_client
        self._event_translator = event_translator
        self.model = model
        self.last_usage: dict[int, ForgeTokenUsage] = {}
        self._model_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._provider_reported = False
        self._in_model_call = False
        self._model_exception_seen = False
        self._pending_call_ids: dict[tuple[str, str], deque[str]] = defaultdict(deque)
        self._model_turn = 0

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        if self._event_translator is None:
            return ()
        return self._event_translator.events

    @property
    def model_calls(self) -> int:
        return self._model_calls

    @property
    def token_usage(self) -> TokenUsage | None:
        if self._input_tokens == 0 and self._output_tokens == 0:
            return None
        return TokenUsage(
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            total_tokens=self._input_tokens + self._output_tokens,
            provider_reported=self._provider_reported,
        )

    @property
    def in_model_call(self) -> bool:
        return self._in_model_call

    @property
    def model_exception_seen(self) -> bool:
        return self._model_exception_seen

    async def send(
        self,
        messages: list[dict[str, str]],
        tools: list[ToolSpec] | None = None,
        sampling: dict[str, Any] | None = None,
        passthrough: dict[str, Any] | None = None,
        inbound_anthropic_body: dict[str, Any] | None = None,
        raw_openai_tools: list[dict[str, Any]] | None = None,
    ) -> list[ToolCall] | TextResponse:
        if sampling:
            raise ForgeBridgeError(
                "ForgeModelBridge does not accept sampling overrides"
            )
        if passthrough or inbound_anthropic_body or raw_openai_tools:
            raise ForgeBridgeError(
                "ForgeModelBridge does not accept provider passthrough"
            )
        model_turn = self._model_turn
        self._model_turn += 1
        request = ModelCompletionRequest(
            request_id=self._request_id(model_turn),
            run_id=self._run_id(),
            model_profile_id=self.model,
            messages=tuple(
                _model_message_from_private(message) for message in messages
            ),
            tools=tuple(_tool_definition_from_spec(spec) for spec in tools or ()),
            sampling_overrides=SamplingRequest(),
            maximum_output_tokens_override=None,
            deadline=self._deadline(),
            cancellation=self._cancellation(),
            secret_refs=(
                self._event_translator._session_request.execution_request.secret_refs
                if self._event_translator is not None
                else ()
            ),
        )
        if self._event_translator is not None:
            self._event_translator.emit(
                SessionEventType.MODEL_REQUEST_STARTED,
                model_turn=model_turn,
                fields={
                    "message_count": len(request.messages),
                    "tool_count": len(request.tools),
                    **_prompt_event_fields(request.messages),
                },
            )
        try:
            self._in_model_call = True
            response = await self._model_client.complete(request)
        except Exception as exc:
            self._model_exception_seen = True
            if self._event_translator is not None:
                self._event_translator.emit(
                    SessionEventType.MODEL_REQUEST_FAILED,
                    model_turn=model_turn,
                    code=type(exc).__name__,
                )
            raise
        finally:
            self._in_model_call = False
        if self._event_translator is not None:
            self._event_translator.emit(
                SessionEventType.MODEL_REQUEST_COMPLETED,
                model_turn=model_turn,
                fields={"tool_call_count": len(response.tool_calls)},
            )
        self._store_usage(response.usage)
        if not response.tool_calls:
            return TextResponse(response.content or "")
        if (
            not request.required_capabilities.parallel_tool_calls
            and len(response.tool_calls) > 1
        ):
            raise ForgeBridgeError("ForgeModelBridge rejects parallel tool calls")
        calls: list[ToolCall] = []
        for call in response.tool_calls:
            args = _parsed_model_tool_args(call)
            if isinstance(args, dict):
                self._pending_call_ids[_tool_call_signature(call.name, args)].append(
                    call.call_id
                )
            calls.append(
                ToolCall(tool=call.name, args=args, reasoning=response.content)
            )
        return calls

    async def send_stream(
        self,
        messages: list[dict[str, str]],
        tools: list[ToolSpec] | None = None,
        sampling: dict[str, Any] | None = None,
        passthrough: dict[str, Any] | None = None,
        inbound_anthropic_body: dict[str, Any] | None = None,
        raw_openai_tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        _ = (
            messages,
            tools,
            sampling,
            passthrough,
            inbound_anthropic_body,
            raw_openai_tools,
        )
        raise NotImplementedError("ForgeModelBridge explicitly rejects streaming")
        if False:
            yield StreamChunk(type="final")  # pragma: no cover

    async def get_context_length(self) -> int | None:
        return None

    async def aclose(self) -> None:
        return None

    def resolve_tool_call_id(
        self, tool_name: str, args: Mapping[str, Any]
    ) -> str | None:
        queue = self._pending_call_ids.get(_tool_call_signature(tool_name, args))
        if not queue:
            return None
        return queue.popleft()

    def _request_id(self, model_turn: int) -> str:
        if self._event_translator is None:
            return f"model-request-{model_turn}"
        return self._event_translator._session_request.execution_request.request_id

    def _run_id(self) -> str:
        if self._event_translator is None:
            return "model-run"
        return self._event_translator._session_request.execution_request.run_id

    def _deadline(self) -> Deadline:
        if self._event_translator is not None:
            return self._event_translator._session_request.deadline
        return Deadline(
            started_monotonic=0.0,
            outer_deadline_monotonic=1.0,
            effective_deadline_monotonic=1.0,
            source="request",
        )

    def _cancellation(self) -> CancellationRef:
        if self._event_translator is not None:
            return (
                self._event_translator._session_request.execution_request.cancellation
            )
        return CancellationRef(cancellation_id="model-cancel")

    def _store_usage(self, usage: TokenUsage | None) -> None:
        self._model_calls += 1
        if usage is None:
            self.last_usage = {}
            return
        token_usage = usage
        self._input_tokens += usage.input_tokens
        self._output_tokens += usage.output_tokens
        self._provider_reported = (
            self._provider_reported or token_usage.provider_reported
        )
        self.last_usage = {
            0: ForgeTokenUsage(
                prompt_tokens=token_usage.input_tokens,
                completion_tokens=token_usage.output_tokens,
                total_tokens=token_usage.total_tokens,
            )
        }


class ForgeToolBridge:
    """Private Forge tool callables backed by a public ``ToolExecutor``."""

    def __init__(
        self,
        *,
        plan: CompiledHarnessPlan,
        session_request: GuardedSessionRequest,
        executor: ToolExecutorLike,
        cancellation_resolver: CancellationResolverLike,
        clock: RuntimeClockLike,
        call_id_resolver: Callable[[str, Mapping[str, Any]], str | None] | None = None,
    ) -> None:
        self._plan = plan
        self._session_request = session_request
        self._executor = executor
        self._cancellation_resolver = cancellation_resolver
        self._clock = clock
        self._call_id_resolver = call_id_resolver
        self._nodes_by_tool = {node.model_tool_name: node for node in plan.nodes}
        self._owned_successes: dict[
            str, tuple[ToolExecutionResult, dict[str, Any]]
        ] = {}
        self._owned_artifacts: dict[str, ArtifactRef] = {}
        self._events: list[SessionEvent] = []
        self._tool_trace: list[ToolTraceRecord] = []
        self._terminal_candidate: TerminalCandidate | None = None
        self._terminal_intent: TerminalIntent | None = None
        self._sequence = 0
        self._bridge_call_counter = 0

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        return tuple(self._events)

    @property
    def tool_trace(self) -> tuple[ToolTraceRecord, ...]:
        return tuple(self._tool_trace)

    @property
    def terminal_candidate(self) -> TerminalCandidate | None:
        return self._terminal_candidate

    @property
    def terminal_intent(self) -> TerminalIntent | None:
        return self._terminal_intent

    def make_callable(self, tool_name: str) -> Callable[..., Any]:
        if tool_name not in self._nodes_by_tool:
            raise ForgeBindingRejectedError(
                f"Cannot build callable for tool outside compiled plan: {tool_name!r}"
            )

        async def _callable(**kwargs: Any) -> str:
            return await self.invoke(tool_name, kwargs)

        return _callable

    async def invoke(self, tool_name: str, arguments: Mapping[str, Any]) -> str:
        node = self._nodes_by_tool.get(tool_name)
        if node is None:
            raise NonRetryableToolError(f"Tool {tool_name!r} is outside compiled plan")
        args = dict(arguments)
        call_id = self._resolve_call_id(tool_name, args)
        input_sha256 = _sha256_json(args)
        self._append_event(
            SessionEventType.TOOL_STARTED,
            node_id=node.node_id,
            tool_call_id=call_id,
        )
        prereq_decisions = self._prerequisite_decisions(node, args)
        capability_decisions = self._capability_decisions(node)
        if _has_denial((*prereq_decisions, *capability_decisions)):
            self._append_trace(
                node=node,
                call_id=call_id,
                input_sha256=input_sha256,
                prerequisite_decisions=prereq_decisions,
                capability_decisions=capability_decisions,
                status=ToolExecutionStatus.NOT_EXECUTED,
                retryable=False,
                side_effect_certainty=SideEffectCertainty.NOT_ATTEMPTED,
                summary="tool rejected before execution",
            )
            self._append_event(
                SessionEventType.PREREQUISITE_REJECTED,
                node_id=node.node_id,
                tool_call_id=call_id,
            )
            raise ToolResolutionError(
                "Tool prerequisites or capabilities are not satisfied",
                tool_name=tool_name,
            )

        token = self._cancellation_resolver.resolve(
            self._session_request.execution_request.cancellation
        )
        if token.is_cancelled():
            self._append_trace(
                node=node,
                call_id=call_id,
                input_sha256=input_sha256,
                prerequisite_decisions=prereq_decisions,
                capability_decisions=capability_decisions,
                status=ToolExecutionStatus.CANCELLED,
                retryable=False,
                side_effect_certainty=SideEffectCertainty.NOT_ATTEMPTED,
                summary=token.reason or "tool call cancelled",
            )
            self._append_event(
                SessionEventType.CANCELLED,
                node_id=node.node_id,
                tool_call_id=call_id,
            )
            raise NonRetryableToolError(token.reason or "tool call cancelled")
        if self._session_request.deadline.remaining(self._clock) <= 0:
            self._append_trace(
                node=node,
                call_id=call_id,
                input_sha256=input_sha256,
                prerequisite_decisions=prereq_decisions,
                capability_decisions=capability_decisions,
                status=ToolExecutionStatus.TIMED_OUT,
                retryable=False,
                side_effect_certainty=SideEffectCertainty.NOT_ATTEMPTED,
                summary="tool call deadline expired",
            )
            self._append_event(
                SessionEventType.TIMED_OUT,
                node_id=node.node_id,
                tool_call_id=call_id,
            )
            raise NonRetryableToolError("tool call deadline expired")

        validated_call = ValidatedToolCall(
            call_id=call_id,
            node_id=node.node_id,
            binding=node.binding,
            arguments=args,
        )
        try:
            result = await self._executor.execute(
                validated_call, self._execution_context()
            )
        except Exception as exc:
            self._append_trace(
                node=node,
                call_id=call_id,
                input_sha256=input_sha256,
                prerequisite_decisions=prereq_decisions,
                capability_decisions=capability_decisions,
                status=ToolExecutionStatus.HARD_FAILURE,
                retryable=False,
                side_effect_certainty=SideEffectCertainty.COMPLETION_UNKNOWN,
                summary=f"tool implementation defect: {type(exc).__name__}",
            )
            self._append_event(
                SessionEventType.TOOL_FAILED,
                node_id=node.node_id,
                tool_call_id=call_id,
                code="implementation_defect",
            )
            raise

        result_defect = _tool_result_boundary_defect(
            result=result,
            expected_call=validated_call,
            expected_input_sha256=input_sha256,
        )
        if result_defect is not None:
            self._append_trace(
                node=node,
                call_id=call_id,
                input_sha256=input_sha256,
                prerequisite_decisions=prereq_decisions,
                capability_decisions=capability_decisions,
                status=ToolExecutionStatus.HARD_FAILURE,
                retryable=False,
                side_effect_certainty=SideEffectCertainty.COMPLETION_UNKNOWN,
                summary=f"tool binding defect: {result_defect}",
                duration_ms=result.duration_ms,
            )
            self._append_event(
                SessionEventType.TOOL_FAILED,
                node_id=node.node_id,
                tool_call_id=call_id,
                code="binding_defect",
            )
            raise NonRetryableToolError(f"Tool {result_defect}")
        try:
            output_sha256 = _validate_output_hash(result)
        except NonRetryableToolError:
            self._append_trace(
                node=node,
                call_id=call_id,
                input_sha256=input_sha256,
                prerequisite_decisions=prereq_decisions,
                capability_decisions=capability_decisions,
                status=ToolExecutionStatus.HARD_FAILURE,
                retryable=False,
                side_effect_certainty=SideEffectCertainty.COMPLETION_UNKNOWN,
                output_sha256=result.output_sha256,
                duration_ms=result.duration_ms,
                summary="tool binding defect: output_sha256 mismatch",
            )
            self._append_event(
                SessionEventType.TOOL_FAILED,
                node_id=node.node_id,
                tool_call_id=call_id,
                code="output_hash_mismatch",
            )
            raise
        status = result.status
        self._append_trace(
            node=node,
            call_id=call_id,
            input_sha256=input_sha256,
            prerequisite_decisions=prereq_decisions,
            capability_decisions=capability_decisions,
            status=status,
            retryable=result.retryable,
            side_effect_certainty=result.side_effect_certainty,
            side_effect_class=result.side_effect_class,
            idempotency=result.idempotency,
            output_sha256=output_sha256,
            duration_ms=result.duration_ms,
            summary=result.summary,
        )
        if result.status != ToolExecutionStatus.SUCCESS:
            self._append_event(
                SessionEventType.TOOL_FAILED,
                node_id=node.node_id,
                tool_call_id=call_id,
                code=result.error_code,
            )
            message = _failure_message(result)
            if result.retryable:
                raise ToolResolutionError(
                    message,
                    tool_name=tool_name,
                )
            raise NonRetryableToolError(message)

        self._owned_successes[node.node_id] = (result, args)
        for artifact_ref in result.artifact_refs:
            self._owned_artifacts[artifact_ref.artifact_id] = artifact_ref
        self._append_event(
            SessionEventType.TOOL_COMPLETED,
            node_id=node.node_id,
            tool_call_id=call_id,
        )
        if node.terminal_result is not None:
            self._accept_terminal_candidate(node, call_id, result)
        return _model_visible_tool_content(result)

    def _resolve_call_id(self, tool_name: str, args: Mapping[str, Any]) -> str:
        if self._call_id_resolver is not None:
            resolved = self._call_id_resolver(tool_name, args)
            if resolved is not None:
                return resolved
        call_id = f"bridge_call_{self._bridge_call_counter:09d}"
        self._bridge_call_counter += 1
        return call_id

    def _execution_context(self) -> ToolExecutionContext:
        request = self._session_request.execution_request
        return ToolExecutionContext(
            request_id=request.request_id,
            run_id=request.run_id,
            stage=request.stage,
            run_directory=request.run_directory,
            capability_envelope=request.capability_envelope,
            timeout=request.timeout,
            cancellation=request.cancellation,
            deadline=self._session_request.deadline,
        )

    def _prerequisite_decisions(
        self, node: CompiledHarnessNode, args: Mapping[str, Any]
    ) -> tuple[ToolTraceDecisionRecord, ...]:
        decisions: list[ToolTraceDecisionRecord] = []
        for prereq in node.prerequisites:
            prior = self._owned_successes.get(prereq.node_id)
            satisfied = prior is not None
            prior_args = prior[1] if prior else {}
            for match in prereq.argument_matches:
                satisfied = satisfied and prior_args.get(
                    match.prerequisite_argument
                ) == args.get(match.current_argument)
            decisions.append(
                ToolTraceDecisionRecord(
                    key=prereq.node_id,
                    decision=ToolTraceDecision.ALLOWED
                    if satisfied
                    else ToolTraceDecision.DENIED,
                )
            )
        return tuple(decisions)

    def _capability_decisions(
        self, node: CompiledHarnessNode
    ) -> tuple[ToolTraceDecisionRecord, ...]:
        granted = {
            grant.capability_id
            for grant in self._session_request.execution_request.capability_envelope.grants
        }
        decisions = [
            ToolTraceDecisionRecord(
                key=capability,
                decision=ToolTraceDecision.ALLOWED
                if capability in granted
                else ToolTraceDecision.DENIED,
            )
            for capability in node.required_capabilities
        ]
        if not self._executor.supports_tool(node.model_tool_name):
            decisions.append(
                ToolTraceDecisionRecord(
                    key=f"tool:{node.model_tool_name}",
                    decision=ToolTraceDecision.DENIED,
                )
            )
        return tuple(decisions)

    def _accept_terminal_candidate(
        self,
        node: CompiledHarnessNode,
        call_id: str,
        result: ToolExecutionResult,
    ) -> None:
        assert node.terminal_result is not None
        candidate = TerminalCandidate(
            call_id=call_id,
            node_id=node.node_id,
            tool_name=node.model_tool_name,
            terminal_result=node.terminal_result,
            summary=result.summary,
            artifact_refs=tuple(self._owned_artifacts.values()),
        )
        self._terminal_candidate = candidate
        missing_required = [
            item.node_id
            for item in self._plan.nodes
            if item.required and item.node_id not in self._owned_successes
        ]
        required_artifacts = {
            artifact_id
            for requirement in self._plan.artifact_policy.required_by_terminal
            if requirement.terminal_result == node.terminal_result
            for artifact_id in requirement.artifact_ids
        }
        missing_artifacts = sorted(required_artifacts - set(self._owned_artifacts))
        mapping_valid = (
            self._plan.terminal_result_map.get(node.node_id) == node.terminal_result
        )
        if missing_required or missing_artifacts or not mapping_valid:
            self._append_event(
                SessionEventType.TERMINAL_INTENT_REJECTED,
                node_id=node.node_id,
                tool_call_id=call_id,
            )
            raise NonRetryableToolError(
                "Terminal candidate failed owned-history validation"
            )
        request = self._session_request.execution_request
        self._terminal_intent = TerminalIntent(
            request_id=request.request_id,
            run_id=request.run_id,
            stage=request.stage,
            terminal_node_id=node.node_id,
            terminal_result=node.terminal_result,
            disposition="success",
            summary=candidate.summary,
            artifact_refs=candidate.artifact_refs,
        )
        self._append_event(
            SessionEventType.TERMINAL_INTENT_ACCEPTED,
            node_id=node.node_id,
            tool_call_id=call_id,
        )

    def _append_event(
        self,
        event_type: SessionEventType,
        *,
        node_id: str | None = None,
        tool_call_id: str | None = None,
        code: str | None = None,
    ) -> None:
        self._sequence += 1
        request = self._session_request.execution_request
        self._events.append(
            SessionEvent(
                schema_version="1.0",
                sequence=self._sequence,
                occurred_at=self._clock.utc_now().isoformat(),
                monotonic_offset_ms=self._clock.monotonic() * 1000,
                event_type=event_type,
                request_id=request.request_id,
                run_id=request.run_id,
                session_id=self._session_request.session_id,
                stage=request.stage,
                node_id=node_id,
                model_turn=None,
                tool_call_id=tool_call_id,
                code=code,
                fields=(),
            )
        )

    def _append_trace(
        self,
        *,
        node: CompiledHarnessNode,
        call_id: str,
        input_sha256: str,
        prerequisite_decisions: tuple[ToolTraceDecisionRecord, ...],
        capability_decisions: tuple[ToolTraceDecisionRecord, ...],
        status: ToolExecutionStatus,
        retryable: bool,
        side_effect_certainty: SideEffectCertainty,
        summary: str,
        side_effect_class: SideEffectClass | None = None,
        idempotency: IdempotencyClass | None = None,
        output_sha256: str | None = None,
        duration_ms: float = 0.0,
    ) -> None:
        request = self._session_request.execution_request
        self._tool_trace.append(
            ToolTraceRecord(
                schema_version="1.0",
                sequence=len(self._tool_trace) + 1,
                occurred_at=self._clock.utc_now().isoformat(),
                monotonic_offset_ms=self._clock.monotonic() * 1000,
                request_id=request.request_id,
                run_id=request.run_id,
                session_id=self._session_request.session_id,
                stage=request.stage,
                node_id=node.node_id,
                model_turn=0,
                tool_call_id=call_id,
                model_tool_name=node.model_tool_name,
                binding=node.binding,
                input_sha256=input_sha256,
                prerequisite_decisions=prerequisite_decisions,
                capability_decisions=capability_decisions,
                execution_status=status,
                retryable=retryable,
                side_effect_class=_trace_side_effect(
                    side_effect_class or node.side_effect_class
                ),
                idempotency=_trace_idempotency(idempotency or node.idempotency),
                side_effect_certainty=side_effect_certainty,
                output_sha256=output_sha256,
                duration_ms=duration_ms,
                summary=summary[:2048] or "tool trace",
            )
        )


def cancellation_event_from_ref(
    cancel_requested: bool,
) -> asyncio.Event:
    """Create a private Forge cancellation event from resolved cancellation state."""

    event = asyncio.Event()
    if cancel_requested:
        event.set()
    return event


def _reject(message: str) -> None:
    raise ForgeBindingRejectedError(message)


def _tool_spec_from_node(node: CompiledHarnessNode) -> ToolSpec:
    try:
        return ToolSpec.from_json_schema(
            node.model_tool_name,
            node.description,
            node.input_schema,
        )
    except ValueError as exc:
        _reject(f"Unsupported input schema for node {node.node_id!r}: {exc}")


def _translate_prerequisite(
    prereq: CompiledPrerequisite,
    tool_name_by_node_id: Mapping[str, str],
) -> str | dict[str, str]:
    prereq_tool = tool_name_by_node_id.get(prereq.node_id)
    if prereq_tool is None:
        _reject(f"Prerequisite references unknown node {prereq.node_id!r}")
    if not prereq.argument_matches:
        return prereq_tool
    match = _single_supported_argument_match(prereq.argument_matches)
    if match.prerequisite_argument == match.current_argument:
        return {"tool": prereq_tool, "match_arg": match.current_argument}
    return {
        "tool": prereq_tool,
        "prerequisite_arg": match.prerequisite_argument,
        "current_arg": match.current_argument,
    }


def _single_supported_argument_match(
    matches: tuple[ArgumentMatch, ...],
) -> ArgumentMatch:
    if len(matches) != 1:
        _reject("Forge prerequisites support at most one argument match")
    return matches[0]


def _validate_plan_identities(plan: CompiledHarnessPlan) -> None:
    _unique_or_reject((node.node_id for node in plan.nodes), "node_id")
    _unique_or_reject((node.model_tool_name for node in plan.nodes), "model_tool_name")
    _unique_or_reject(
        (f"{node.binding.tool_id}:{node.binding.tool_version}" for node in plan.nodes),
        "binding identity",
    )
    node_ids = {node.node_id for node in plan.nodes}
    terminal_node_ids = {
        node.node_id for node in plan.nodes if node.terminal_result is not None
    }
    if set(plan.terminal_result_map) != terminal_node_ids:
        _reject("terminal_result_map must exactly name terminal nodes")
    for node in plan.nodes:
        if not node.node_id or not node.model_tool_name:
            _reject("Compiled node identities must be non-empty")
        for prereq in node.prerequisites:
            if prereq.node_id not in node_ids:
                _reject(f"Prerequisite references unknown node {prereq.node_id!r}")


def _unique_or_reject(values: Any, label: str) -> None:
    items = tuple(values)
    if len(set(items)) != len(items):
        _reject(f"Duplicate {label} values are unsupported")


def _session_payload(
    plan: CompiledHarnessPlan,
    session_request: GuardedSessionRequest,
    request: HarnessExecutionRequest,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "millforge_stage_request",
        "request_id": request.request_id,
        "run_id": request.run_id,
        "schema_version": "1.0",
        "stage": {
            "node_id": request.stage.node_id,
            "plane": request.stage.plane,
            "stage_kind_id": request.stage.stage_kind_id,
        },
    }
    if plan.prompt_policy.include_request_context:
        payload["work_item_id"] = request.work_item_id
        payload["input_artifacts"] = [
            {
                "artifact_id": artifact.artifact_id,
                "content_type": artifact.content_type,
                "path": _path_text(artifact.path),
            }
            for artifact in request.input_artifacts
        ]
    return payload


def _context_compaction_callback(_event: Any) -> None:
    return None


def _path_text(path: Path) -> str:
    return path.as_posix()


def _model_message_from_private(message: Mapping[str, Any]) -> ModelMessage:
    role = message.get("role")
    content = message.get("content")
    if not isinstance(role, str):
        raise ForgeBridgeError("private message role is missing")
    tool_calls: list[ModelToolCall] = []
    for index, raw_call in enumerate(message.get("tool_calls") or ()):
        function = raw_call.get("function", {}) if isinstance(raw_call, dict) else {}
        args = function.get("arguments", {})
        call_id = raw_call.get("id") or f"private_call_{index:09d}"
        tool_calls.append(
            ModelToolCall(
                call_id=str(call_id),
                name=str(function.get("name", "")),
                arguments=_tool_arguments_from_private(args),
            )
        )
    if role == "system":
        return SystemMessage(content=str(content or ""))
    if role == "user":
        return UserMessage(content=str(content or ""))
    if role == "assistant":
        return AssistantMessage(
            content=str(content) if content is not None else None,
            tool_calls=tuple(tool_calls),
        )
    if role == "tool":
        return ToolResultMessage(
            tool_call_id=str(message.get("tool_call_id") or ""),
            tool_name=str(message.get("tool_name") or message.get("name") or ""),
            content=str(content or ""),
        )
    raise ForgeBridgeError(f"Unsupported private message role {role!r}")


def _prompt_event_fields(
    messages: tuple[ModelMessage, ...],
) -> dict[str, str | int]:
    fields: dict[str, str | int] = {}
    for index, message in enumerate(messages):
        content = message.content or ""
        encoded = content.encode("utf-8")
        prefix = f"prompt_{index}"
        fields[f"{prefix}_role"] = message.role
        fields[f"{prefix}_byte_size"] = len(encoded)
        fields[f"{prefix}_sha256"] = hashlib.sha256(encoded).hexdigest()
    return fields


def _tool_arguments_from_private(
    value: Any,
) -> ParsedToolArguments | InvalidToolArguments:
    if isinstance(value, dict):
        return ParsedToolArguments(value=value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return InvalidToolArguments(
                raw=value,
                error_code="invalid_json",
            )
        if isinstance(parsed, dict):
            return ParsedToolArguments(value=parsed)
    return InvalidToolArguments(
        raw=value,
        error_code="invalid_arguments",
    )


def _tool_definition_from_spec(spec: ToolSpec) -> ModelToolDefinition:
    return ModelToolDefinition(
        name=spec.name,
        description=spec.description,
        input_schema=spec.get_json_schema(),
    )


def _parsed_model_tool_args(call: ModelToolCall) -> Any:
    if isinstance(call.arguments, ParsedToolArguments):
        return dict(call.arguments.value)
    return call.arguments.raw


def _tool_call_signature(tool_name: str, args: Mapping[str, Any]) -> tuple[str, str]:
    return tool_name, canonical_json_serialize(dict(args))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_serialize(value).encode("utf-8")).hexdigest()


def _validate_output_hash(result: ToolExecutionResult) -> str | None:
    computed = _sha256_json(_safe_output_payload(result))
    if result.output_sha256 is not None and result.output_sha256 != computed:
        raise NonRetryableToolError(
            "Tool result output_sha256 did not match safe output"
        )
    return result.output_sha256 or computed


def _tool_result_boundary_defect(
    *,
    result: ToolExecutionResult,
    expected_call: ValidatedToolCall,
    expected_input_sha256: str,
) -> str | None:
    if result.call_id != expected_call.call_id:
        return "result call_id mismatch"
    if result.input_sha256 != expected_input_sha256:
        return "result input_sha256 mismatch"
    return None


def _safe_output_payload(result: ToolExecutionResult) -> dict[str, Any]:
    return {
        "summary": result.summary,
        "structured_data": result.structured_data,
        "artifact_refs": [ref.model_dump(mode="json") for ref in result.artifact_refs],
    }


def _trace_side_effect(value: SideEffectClass) -> ToolTraceSideEffectClass:
    return ToolTraceSideEffectClass(value.value)


def _trace_idempotency(value: IdempotencyClass) -> ToolTraceIdempotency:
    return ToolTraceIdempotency(value.value)


def _has_denial(records: tuple[ToolTraceDecisionRecord, ...]) -> bool:
    return any(record.decision == ToolTraceDecision.DENIED for record in records)


def _model_visible_tool_content(result: ToolExecutionResult) -> str:
    if result.status == ToolExecutionStatus.SUCCESS:
        return result.summary
    return _failure_message(result)


def _failure_message(result: ToolExecutionResult) -> str:
    code = result.error_code or "tool_failed"
    return f"[{code}] {result.summary}"


def _diagnostic_fields(
    fields: Mapping[str, str | int | float | bool | None],
) -> tuple[DiagnosticField, ...]:
    return tuple(
        DiagnosticField(
            key=str(key),
            value=value[:256] if isinstance(value, str) else value,
        )
        for key, value in fields.items()
    )


def _timing_metadata(started_at: Any, completed_at: Any) -> TimingMetadata:
    return TimingMetadata(
        started_at=started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        completed_at=completed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        duration_ms=max(0.0, (completed_at - started_at).total_seconds() * 1000.0),
    )


def _single_event(
    translator: ForgeEventTranslator,
    event_type: SessionEventType,
    *,
    code: str | None = None,
) -> tuple[SessionEvent, ...]:
    return (translator.emit(event_type, code=code),)


def _ordered_events(
    *groups: tuple[SessionEvent, ...],
) -> tuple[SessionEvent, ...]:
    events = [event for group in groups for event in group]
    return tuple(
        event.model_copy(update={"sequence": sequence})
        for sequence, event in enumerate(events, start=1)
    )


def _usage_from_bridges(
    model_bridge: ForgeModelBridge,
    tool_bridge: ForgeToolBridge,
) -> UsageMetadata:
    executed_tool_calls = sum(
        1
        for trace in tool_bridge.tool_trace
        if trace.execution_status != ToolExecutionStatus.NOT_EXECUTED
    )
    return UsageMetadata(
        model_calls=model_bridge.model_calls,
        tool_calls=executed_tool_calls,
        token_usage=model_bridge.token_usage,
    )


__all__ = [
    "ForgeGuardrailBackend",
    "ForgeBindingRejectedError",
    "ForgeBridgeError",
    "ForgeContextFactory",
    "ForgeEventTranslator",
    "ForgeModelBridge",
    "ForgeRunnerOptions",
    "ForgeSessionInputBuilder",
    "ForgeToolBridge",
    "ForgeWorkflowFactory",
    "ForgeWorkflowInput",
    "TerminalCandidate",
    "cancellation_event_from_ref",
]
