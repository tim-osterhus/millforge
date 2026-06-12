from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from millforge._forge.context.manager import ContextManager
from millforge._forge.context.strategies import NoCompact
from millforge._forge.core.messages import Message
from millforge._forge.core.steps import StepTracker
from millforge._forge.core.workflow import (
    LLMResponse,
    TextResponse,
    ToolCall,
    ToolDef,
    ToolSpec,
    Workflow,
)
from millforge._forge.core.runner import WorkflowRunner
from millforge._forge.errors import (
    NonRetryableToolError,
    PrerequisiteError,
    StepEnforcementError,
    ToolExecutionError,
)


class EmptyParams(BaseModel):
    pass


class MockClient:
    def __init__(self, responses: list[ToolCall | TextResponse]) -> None:
        self.responses = list(responses)
        self._call_index = 0
        self.send_calls: list[tuple[list[dict[str, str]], list[ToolSpec] | None]] = []

    def _next(self) -> LLMResponse:
        response = self.responses[self._call_index]
        self._call_index += 1
        if isinstance(response, ToolCall):
            return [response]
        return response

    async def send(
        self,
        messages: list[dict[str, str]],
        tools: list[ToolSpec] | None = None,
        sampling: dict[str, object] | None = None,
        passthrough: dict[str, object] | None = None,
        inbound_anthropic_body: dict[str, object] | None = None,
    ) -> LLMResponse:
        self.send_calls.append((messages, tools))
        return self._next()

    async def get_context_length(self) -> int | None:
        return None


def _make_tool(
    name: str,
    fn: Callable[..., Any] | None = None,
    prerequisites: list[str | dict[str, str]] | None = None,
) -> ToolDef:
    if fn is None:

        def default_tool(**kwargs: Any) -> str:
            return f"{name}_result"

        fn = default_tool
    return ToolDef(
        spec=ToolSpec(name=name, description=f"Tool {name}", parameters=EmptyParams),
        callable=fn,
        prerequisites=prerequisites or [],
    )


def _make_workflow(
    tools: dict[str, ToolDef] | None = None,
    required_steps: list[str] | None = None,
    terminal_tool: str = "submit",
) -> Workflow:
    if tools is None:
        tools = {
            "fetch": _make_tool("fetch"),
            "submit": _make_tool("submit"),
        }
    if required_steps is None:
        required_steps = ["fetch"]
    return Workflow(
        name="test_wf",
        description="A test workflow",
        tools=tools,
        required_steps=required_steps,
        terminal_tool=terminal_tool,
        system_prompt_template="You are a {role}.",
    )


def _make_runner(
    client: MockClient,
    *,
    max_iterations: int = 10,
    max_premature_attempts: int = 3,
    max_prereq_violations: int = 2,
    on_message: Callable[[Message], None] | None = None,
) -> WorkflowRunner:
    return WorkflowRunner(
        client=client,
        context_manager=ContextManager(strategy=NoCompact(), budget_tokens=100_000),
        max_iterations=max_iterations,
        max_premature_attempts=max_premature_attempts,
        max_prereq_violations=max_prereq_violations,
        on_message=on_message,
    )


def test_step_tracker_matches_distinct_prerequisite_and_current_arguments() -> None:
    tracker = StepTracker(required_steps=[])
    prerequisite = {
        "tool": "lookup",
        "prerequisite_arg": "source_path",
        "current_arg": "path",
    }

    tracker.record("lookup", {"source_path": "input.json"})

    assert tracker.check_prerequisites(
        "submit", {"path": "input.json"}, [prerequisite]
    ).satisfied
    missing = tracker.check_prerequisites(
        "submit", {"path": "other.json"}, [prerequisite]
    )
    assert missing.satisfied is False
    assert missing.missing == ["lookup"]


@pytest.mark.asyncio
async def test_runner_applies_custom_premature_terminal_attempt_limit() -> None:
    client = MockClient(
        [
            ToolCall(tool="submit", args={}),
            ToolCall(tool="submit", args={}),
        ]
    )
    runner = _make_runner(client, max_premature_attempts=1)

    with pytest.raises(StepEnforcementError) as exc_info:
        await runner.run(_make_workflow(), "go", prompt_vars={"role": "agent"})

    assert exc_info.value.terminal_tool == "submit"
    assert exc_info.value.attempts == 2
    assert len(client.send_calls) == 2


@pytest.mark.asyncio
async def test_runner_keeps_default_premature_terminal_attempt_limit() -> None:
    client = MockClient(
        [
            ToolCall(tool="submit", args={}),
            ToolCall(tool="submit", args={}),
            ToolCall(tool="submit", args={}),
            ToolCall(tool="fetch", args={}),
            ToolCall(tool="submit", args={}),
        ]
    )
    runner = _make_runner(client)

    result = await runner.run(_make_workflow(), "go", prompt_vars={"role": "agent"})

    assert result == "submit_result"


@pytest.mark.asyncio
async def test_runner_applies_custom_prerequisite_violation_limit() -> None:
    tools = {
        "fetch": _make_tool("fetch"),
        "read": _make_tool("read", prerequisites=["fetch"]),
        "submit": _make_tool("submit"),
    }
    workflow = _make_workflow(tools=tools, required_steps=[], terminal_tool="submit")
    client = MockClient(
        [
            ToolCall(tool="read", args={}),
            ToolCall(tool="read", args={}),
        ]
    )
    runner = _make_runner(client, max_prereq_violations=1)

    with pytest.raises(PrerequisiteError) as exc_info:
        await runner.run(workflow, "go", prompt_vars={"role": "agent"})

    assert exc_info.value.tool_name == "read"
    assert exc_info.value.violations == 2
    assert exc_info.value.missing_prereqs == ["fetch"]


@pytest.mark.asyncio
async def test_non_retryable_tool_error_bypasses_generic_correction() -> None:
    messages: list[Message] = []

    def fail_non_retryable(**kwargs: Any) -> str:
        raise NonRetryableToolError("quota denied")

    tools = {
        "fetch": _make_tool("fetch", fn=fail_non_retryable),
        "submit": _make_tool("submit"),
    }
    client = MockClient(
        [
            ToolCall(tool="fetch", args={}),
            ToolCall(tool="fetch", args={}),
        ]
    )
    runner = _make_runner(client, on_message=messages.append)

    with pytest.raises(NonRetryableToolError, match="quota denied"):
        await runner.run(
            _make_workflow(tools=tools), "go", prompt_vars={"role": "agent"}
        )

    assert len(client.send_calls) == 1
    assert not any("[ToolError]" in message.content for message in messages)


@pytest.mark.asyncio
async def test_generic_tool_error_still_feeds_back_before_exhaustion() -> None:
    def fail_retryable(**kwargs: Any) -> str:
        raise RuntimeError("temporary")

    tools = {
        "fetch": _make_tool("fetch", fn=fail_retryable),
        "submit": _make_tool("submit"),
    }
    client = MockClient(
        [
            ToolCall(tool="fetch", args={}),
            ToolCall(tool="fetch", args={}),
            ToolCall(tool="fetch", args={}),
        ]
    )
    runner = WorkflowRunner(
        client=client,
        context_manager=ContextManager(strategy=NoCompact(), budget_tokens=100_000),
        max_tool_errors=2,
    )

    with pytest.raises(ToolExecutionError) as exc_info:
        await runner.run(
            _make_workflow(tools=tools), "go", prompt_vars={"role": "agent"}
        )

    assert isinstance(exc_info.value.cause, RuntimeError)
    assert (
        "[ToolError] RuntimeError: temporary" in client.send_calls[1][0][-1]["content"]
    )


def test_strict_json_schema_accepts_supported_v1_subset() -> None:
    spec = ToolSpec.from_json_schema(
        "create_item",
        "Create item",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "description": "Item name"},
                "quantity": {"type": "integer", "default": 1},
                "tags": {"type": "array", "items": {"type": "string"}},
                "mode": {"type": "string", "enum": ["draft", "final"]},
                "metadata": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"source": {"type": "string"}},
                    "required": ["source"],
                },
            },
            "required": ["name", "metadata"],
        },
    )

    params = spec.parameters(
        name="widget",
        metadata={"source": "fixture"},
        tags=["a"],
        mode="draft",
    )

    assert params.name == "widget"
    assert params.quantity == 1
    assert params.metadata.source == "fixture"
    with pytest.raises(ValidationError):
        spec.parameters(name="widget", metadata={})
    with pytest.raises(ValidationError):
        spec.parameters(name="widget", metadata={"source": "fixture"}, extra=True)


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"name": {"type": "string", "minLength": 3}},
                "required": ["name"],
            },
            "minLength",
        ),
        (
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": True,
            },
            "additionalProperties",
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"value": {"type": "null"}},
                "required": ["value"],
            },
            "null",
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"values": {"type": "array"}},
                "required": ["values"],
            },
            "missing items",
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"name": {"type": "string"}},
                "required": ["missing"],
            },
            "Required field",
        ),
    ],
)
def test_strict_json_schema_rejects_unsupported_features_before_use(
    schema: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ToolSpec.from_json_schema("bad", "Bad", schema)
