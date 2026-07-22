"""Exercise an installed wheel or sdist through Millforge's public live facade."""

from __future__ import annotations

import asyncio
import datetime
import importlib.metadata
import json
import sys
import tempfile
from pathlib import Path

import httpx
import millforge
from millforge import (
    AuthenticationPolicy,
    AuthenticationScheme,
    CancellationRef,
    CapabilityDeclarations,
    CapabilitySupport,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    CompiledHarnessRef,
    EndpointConfig,
    ExecutionStatus,
    HarnessExecutionRequest,
    HarnessExecutionResult,
    HarnessTaskInput,
    MillforgeBaseLiveRunner,
    MillforgeBaseOptions,
    MillforgeInvocationEvidence,
    ModelProfileRef,
    OpenAICompatibleTimeouts,
    ReasoningMode,
    ReasoningPolicy,
    RequestOptionAllowlist,
    ResolvedModelProfile,
    ResolvedSecret,
    RunDirRef,
    SecretRef,
    SelectedOutputRequirement,
    StageIdentity,
    TerminalSelectedOutputRequirement,
    TimeoutRef,
    create_millforge_base_live_runner,
)


_RAW_SECRET = "package-smoke-secret-value-7e4f0d"
_TRANSCRIPT_MARKERS = (
    "Read the package note.",
    "Installed package traversal complete.",
)
_REASONING_CONTINUATION = "package-smoke-required-continuation"
_SELECTED_OUTPUT_REQUIREMENTS = (
    TerminalSelectedOutputRequirement(
        terminal_result="BLOCKED",
        selected_output=SelectedOutputRequirement(
            required=True,
            json_schema={"type": "array", "items": {"type": "string"}},
        ),
    ),
    TerminalSelectedOutputRequirement(
        terminal_result="COMPLETE",
        selected_output=SelectedOutputRequirement(
            required=True,
            json_schema={
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        ),
    ),
)


class _CancellationToken:
    def __init__(self, cancellation_id: str) -> None:
        self._cancellation_id = cancellation_id
        self._event = asyncio.Event()

    @property
    def cancellation_id(self) -> str:
        return self._cancellation_id

    def is_cancelled(self) -> bool:
        return False

    async def wait(self) -> None:
        await self._event.wait()

    @property
    def reason(self) -> None:
        return None


class _CancellationResolver:
    def resolve(self, ref: CancellationRef) -> _CancellationToken:
        return _CancellationToken(ref.cancellation_id)


class _Clock:
    def utc_now(self) -> datetime.datetime:
        return datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)

    def monotonic(self) -> float:
        return 1.0


class _SecretResolver:
    def __init__(self, admitted_ref: SecretRef) -> None:
        self._admitted_ref = admitted_ref
        self.resolve_calls = 0

    def resolve(self, ref: SecretRef) -> ResolvedSecret:
        assert ref == self._admitted_ref
        self.resolve_calls += 1
        return ResolvedSecret(_RAW_SECRET)


def _model_profile(secret_ref: SecretRef) -> ResolvedModelProfile:
    return ResolvedModelProfile(
        profile_id="package-smoke-openai-compatible",
        provider_id="openai-compatible",
        model_id="fake-tool-model",
        endpoint=EndpointConfig(base_url="https://models.invalid/v1"),
        authentication=AuthenticationPolicy(
            scheme=AuthenticationScheme.BEARER,
            secret_ref=secret_ref,
        ),
        capabilities=CapabilityDeclarations(
            support={
                "tool_calls": CapabilitySupport.SUPPORTED,
                "system_messages": CapabilitySupport.SUPPORTED,
                "tool_result_messages": CapabilitySupport.SUPPORTED,
                "parallel_tool_calls": CapabilitySupport.UNSUPPORTED,
            }
        ),
        reasoning=ReasoningPolicy(
            mode=ReasoningMode.ENABLED,
            mode_field="thinking",
            mode_values={ReasoningMode.ENABLED: {"type": "enabled"}},
            tool_call_replay_field="reasoning_content",
        ),
        request_options=RequestOptionAllowlist(
            allowed_options=("parallel_tool_calls",),
        ),
        timeout_seconds=30,
        source_digest="installed-package-smoke-v3",
    )


def _tool_call(
    call_id: str, name: str, arguments: dict[str, object]
) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(
                arguments,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    }


def _tool_message(
    content: str,
    *calls: dict[str, object],
    continuation: str = "package-smoke-continuation",
) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": content,
        "reasoning_content": continuation,
        "tool_calls": list(calls),
    }


class _FakeOpenAITransport:
    """Caller-owned scripted transport that never reaches a provider endpoint."""

    def __init__(self, messages: list[dict[str, object]]) -> None:
        self._messages = messages
        self.request_bodies: list[dict[str, object]] = []
        self.close_calls = 0

    @property
    def request_count(self) -> int:
        return len(self.request_bodies)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "fake-tool-model"
        assert body["parallel_tool_calls"] is False
        assert request.url == "https://models.invalid/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {_RAW_SECRET}"
        assert self.request_count < len(self._messages), "unexpected model request"
        self.request_bodies.append(body)
        message = self._messages[self.request_count - 1]
        finish_reason = "tool_calls" if message.get("tool_calls") else "stop"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "model": "fake-tool-model",
                "choices": [
                    {"finish_reason": finish_reason, "message": message},
                ],
            },
            request=request,
        )

    async def aclose(self) -> None:
        self.close_calls += 1


def _request(
    live_runner: MillforgeBaseLiveRunner,
    root: Path,
    secret_ref: SecretRef,
    scenario: str,
    selected_output_requirements: tuple[TerminalSelectedOutputRequirement, ...] = (),
) -> HarnessExecutionRequest:
    plan = live_runner.components.compiled_plan
    run_id = f"run-installed-package-{scenario}"
    return HarnessExecutionRequest(
        request_id=f"request-installed-package-{scenario}",
        run_id=run_id,
        work_item_id=f"work-installed-package-{scenario}",
        task=HarnessTaskInput(instruction="Exercise the installed package offline."),
        stage=StageIdentity(
            plane="execution",
            node_id="millforge-base",
            stage_kind_id="millforge_base",
        ),
        compiled_harness=CompiledHarnessRef(
            identity=CompiledHarnessIdentity(
                compiled_plan_id=f"plan-installed-package-{scenario}",
                harness_id=plan.harness_id,
                harness_version=plan.harness_version,
            ),
            path=root / "compiled-plan.json",
            expected_hash=CompiledHarnessHash(
                algorithm="sha256",
                digest=plan.compiled_sha256,
            ),
        ),
        capability_envelope=live_runner.components.capability_envelope,
        input_artifacts=(),
        run_directory=RunDirRef(run_id=run_id, path=root),
        timeout=TimeoutRef(timeout_seconds=30),
        cancellation=CancellationRef(cancellation_id=f"cancel-{scenario}"),
        secret_refs=(secret_ref,),
        model_profile=ModelProfileRef(profile_id=plan.model_profile.profile_id),
        selected_output_requirements=selected_output_requirements,
    )


def _tool_trace(root: Path) -> list[dict[str, object]]:
    path = root / "millforge" / "tool_trace.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def _execute_scenario(
    root: Path,
    scenario: str,
    messages: list[dict[str, object]],
    selected_output_requirements: tuple[TerminalSelectedOutputRequirement, ...] = (),
) -> tuple[
    HarnessExecutionResult,
    MillforgeInvocationEvidence,
    _FakeOpenAITransport,
    list[dict[str, object]],
]:
    secret_ref = SecretRef(
        secret_id=f"package-smoke-secret-{scenario}",
        env_var="MILLFORGE_PACKAGE_SMOKE_SECRET",
    )
    secret_resolver = _SecretResolver(secret_ref)
    transport = _FakeOpenAITransport(messages)
    live_runner = await create_millforge_base_live_runner(
        profile_id="package-smoke-openai-compatible",
        model_profile=_model_profile(secret_ref),
        secret_ref=secret_ref,
        secret_resolver=secret_resolver,
        cwd=root,
        clock=_Clock(),
        cancellation_resolver=_CancellationResolver(),
        timeouts=OpenAICompatibleTimeouts(
            connect_seconds=2,
            read_seconds=10,
            write_seconds=5,
            pool_seconds=2,
            local_total_seconds=20,
        ),
        http_transport=transport,
        options=MillforgeBaseOptions(load_context_files=False),
        prompt_date=datetime.date(2026, 7, 18),
        home_directory=root,
    )
    request = _request(
        live_runner,
        root,
        secret_ref,
        scenario,
        selected_output_requirements,
    )
    assert transport.request_count == 0
    assert secret_resolver.resolve_calls == 0
    async with live_runner:
        invocation = live_runner.invocation_evidence_for(request)
        result = await live_runner.execute(request)
    assert transport.request_count == len(messages)
    assert transport.close_calls == 0
    assert secret_resolver.resolve_calls == transport.request_count
    trace = _tool_trace(root)
    await transport.aclose()
    assert transport.close_calls == 1
    return result, invocation, transport, trace


def _terminal_evidence(result: HarnessExecutionResult) -> dict[str, object]:
    assert result.status is ExecutionStatus.COMPLETED
    assert result.terminal_intent is not None
    return {
        "schema_sha256": result.selected_output_schema_sha256,
        "selected_output": (
            result.selected_output.model_dump(mode="json")
            if result.selected_output is not None
            else None
        ),
    }


def _message_with_call(body: dict[str, object], call_id: str) -> dict[str, object]:
    messages = body["messages"]
    assert isinstance(messages, list)
    for message in messages:
        if not isinstance(message, dict):
            continue
        calls = message.get("tool_calls", [])
        if isinstance(calls, list) and any(
            isinstance(call, dict) and call.get("id") == call_id for call in calls
        ):
            return message
    raise AssertionError(f"missing assistant tool call {call_id}")


def _tool_result_call_ids(body: dict[str, object]) -> list[str]:
    messages = body["messages"]
    assert isinstance(messages, list)
    return [
        str(message["tool_call_id"])
        for message in messages
        if isinstance(message, dict)
        and message.get("role") == "tool"
        and "tool_call_id" in message
    ]


def _call_history_counts(body: dict[str, object], call_id: str) -> dict[str, int]:
    messages = body["messages"]
    assert isinstance(messages, list)
    assistant_call_records = 0
    tool_result_records = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            calls = message.get("tool_calls", [])
            assert isinstance(calls, list)
            assistant_call_records += sum(
                isinstance(call, dict) and call.get("id") == call_id for call in calls
            )
        elif message.get("role") == "tool" and message.get("tool_call_id") == call_id:
            tool_result_records += 1
    return {
        "assistant_call_records": assistant_call_records,
        "tool_result_records": tool_result_records,
    }


async def _run_public_live_facade() -> tuple[dict[str, object], dict[str, object]]:
    network_events: list[str] = []

    def audit(event: str, _args: tuple[object, ...]) -> None:
        if event in {
            "http.client.connect",
            "socket.connect",
            "socket.getaddrinfo",
            "socket.gethostbyname",
        }:
            network_events.append(event)

    sys.addaudithook(audit)

    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root).resolve()
        (root / "note.txt").write_text("offline package smoke\n", encoding="utf-8")
        (root / "second.txt").write_text("second offline note\n", encoding="utf-8")

        complete, invocation, complete_transport, _ = await _execute_scenario(
            root,
            "complete",
            [
                _tool_message(
                    _TRANSCRIPT_MARKERS[0],
                    _tool_call("call-reasoning-read", "read", {"path": "note.txt"}),
                    continuation=_REASONING_CONTINUATION,
                ),
                _tool_message(
                    _TRANSCRIPT_MARKERS[1],
                    _tool_call(
                        "call-complete",
                        "submit",
                        {
                            "candidate": {"answer": 42},
                            "summary": "offline installed smoke complete",
                            "terminal_result": "COMPLETE",
                        },
                    ),
                ),
            ],
            _SELECTED_OUTPUT_REQUIREMENTS,
        )
        assert complete.terminal_intent is not None
        assert complete.terminal_intent.terminal_result == "COMPLETE"
        replayed = _message_with_call(
            complete_transport.request_bodies[1], "call-reasoning-read"
        )
        assert replayed["reasoning_content"] == _REASONING_CONTINUATION

        blocked, _, _, _ = await _execute_scenario(
            root,
            "blocked",
            [
                _tool_message(
                    "blocked selected output",
                    _tool_call(
                        "call-blocked",
                        "block",
                        {
                            "candidate": ["operator", "input"],
                            "summary": "operator input required",
                            "terminal_result": "BLOCKED",
                        },
                    ),
                )
            ],
            _SELECTED_OUTPUT_REQUIREMENTS,
        )
        assert blocked.terminal_intent is not None
        assert blocked.terminal_intent.terminal_result == "BLOCKED"

        rejected, _, _, _ = await _execute_scenario(
            root,
            "rejected",
            [
                _tool_message(
                    "schema-less rejection",
                    _tool_call(
                        "call-rejected",
                        "reject",
                        {
                            "summary": "request rejected",
                            "terminal_result": "REJECTED",
                        },
                    ),
                )
            ],
        )
        assert rejected.terminal_intent is not None
        assert rejected.status is ExecutionStatus.COMPLETED
        assert rejected.terminal_intent.terminal_result == "REJECTED"
        assert rejected.selected_output is None
        assert rejected.selected_output_schema_sha256 is None

        crossed, _, crossed_transport, crossed_trace = await _execute_scenario(
            root,
            "crossed",
            [
                _tool_message(
                    "crossed selected output",
                    _tool_call(
                        "call-crossed",
                        "submit",
                        {
                            "candidate": ["operator", "input"],
                            "summary": "wrong selected output shape",
                            "terminal_result": "COMPLETE",
                        },
                    ),
                ),
                _tool_message(
                    "correct selected output",
                    _tool_call(
                        "call-crossed-correction",
                        "submit",
                        {
                            "candidate": {"answer": 42},
                            "summary": "corrected selected output",
                            "terminal_result": "COMPLETE",
                        },
                    ),
                ),
            ],
            _SELECTED_OUTPUT_REQUIREMENTS,
        )
        assert crossed.terminal_intent is not None
        assert crossed.terminal_intent.terminal_result == "COMPLETE"
        assert "call-crossed" in _tool_result_call_ids(
            crossed_transport.request_bodies[1]
        )
        crossed_record = next(
            record
            for record in crossed_trace
            if record["tool_call_id"] == "call-crossed"
        )
        assert crossed_record["execution_status"] == "not_executed"

        corrected, _, correction_transport, correction_trace = await _execute_scenario(
            root,
            "soft-failure",
            [
                _tool_message(
                    "missing read",
                    _tool_call(
                        "call-missing-read",
                        "read",
                        {"path": "missing.txt"},
                    ),
                ),
                _tool_message(
                    "corrective read",
                    _tool_call(
                        "call-corrective-read",
                        "read",
                        {"path": "note.txt"},
                    ),
                ),
                _tool_message(
                    "complete after correction",
                    _tool_call(
                        "call-corrected-complete",
                        "submit",
                        {
                            "candidate": {"answer": 42},
                            "summary": "corrected after soft failure",
                            "terminal_result": "COMPLETE",
                        },
                    ),
                ),
            ],
            _SELECTED_OUTPUT_REQUIREMENTS,
        )
        assert corrected.terminal_intent is not None
        assert corrected.terminal_intent.terminal_result == "COMPLETE"
        failed_records = [
            record
            for record in correction_trace
            if record["tool_call_id"] == "call-missing-read"
        ]
        assert len(failed_records) == 1
        failed_record = failed_records[0]
        assert failed_record["execution_status"] == "soft_failure"
        assert failed_record["retryable"] is False
        subsequent_request_history = [
            _call_history_counts(body, "call-missing-read")
            for body in correction_transport.request_bodies[1:]
        ]
        assert subsequent_request_history == [
            {"assistant_call_records": 1, "tool_result_records": 1},
            {"assistant_call_records": 1, "tool_result_records": 1},
        ]

        serial, _, serial_transport, serial_trace = await _execute_scenario(
            root,
            "serial",
            [
                _tool_message(
                    "",
                    _tool_call(
                        "call-serial-first",
                        "read",
                        {"path": "note.txt"},
                    ),
                    _tool_call(
                        "call-serial-second",
                        "read",
                        {"path": "second.txt"},
                    ),
                ),
                _tool_message(
                    "complete serial calls",
                    _tool_call(
                        "call-serial-complete",
                        "submit",
                        {
                            "candidate": {"answer": 42},
                            "summary": "serial calls complete",
                            "terminal_result": "COMPLETE",
                        },
                    ),
                ),
            ],
            _SELECTED_OUTPUT_REQUIREMENTS,
        )
        assert serial.terminal_intent is not None
        serial_assistant = _message_with_call(
            serial_transport.request_bodies[1], "call-serial-first"
        )
        assert "content" not in serial_assistant
        serial_call_ids = [
            str(record["tool_call_id"])
            for record in serial_trace
            if str(record["tool_call_id"]).startswith("call-serial-")
        ]
        assert serial_call_ids[:2] == ["call-serial-first", "call-serial-second"]
        serial_result_ids = _tool_result_call_ids(serial_transport.request_bodies[1])
        assert serial_result_ids[-2:] == ["call-serial-first", "call-serial-second"]

        blank, _, blank_transport, blank_trace = await _execute_scenario(
            root,
            "blank",
            [{"role": "assistant", "content": ""}],
        )
        assert blank.status is not ExecutionStatus.COMPLETED
        assert blank.terminal_intent is None
        assert blank_transport.request_count == 1
        assert blank_trace == []

        descriptor = millforge.describe_millforge_base()
        release_evidence = {
            "identity": {
                "context_contract_version": descriptor.context_contract_version,
                "descriptor_sha256": descriptor.descriptor_sha256,
                "distribution": "millforge",
                "legal_terminal_results": list(descriptor.legal_terminal_result_ids),
                "prompt_contract_version": descriptor.prompt_contract_version,
                "runner_id": descriptor.runner_id,
                "runner_version": descriptor.runner_version,
                "version": millforge.__version__,
            },
            "selected_outputs": {
                "BLOCKED": _terminal_evidence(blocked),
                "COMPLETE": _terminal_evidence(complete),
            },
            "schema_less_terminal_result": {
                "execution_status": rejected.status.value,
                "selected_output": None,
                "selected_output_schema_sha256": None,
                "terminal_result": "REJECTED",
            },
            "crossed_result_refusal": {
                "correction_observed": True,
                "rejected_call_id": "call-crossed",
                "terminal_result": "COMPLETE",
            },
            "selected_output_requirements_sha256": (
                invocation.selected_output_requirements_sha256
            ),
            "reasoning_continuation": {
                "provider_tool_call_id": "call-reasoning-read",
                "replayed": True,
            },
            "soft_failure_correction": {
                "corrective_call_id": "call-corrective-read",
                "failed_execution_trace_records": len(failed_records),
                "failed_call_id": "call-missing-read",
                "failed_call_replayed": len(failed_records) > 1,
                "subsequent_request_history": subsequent_request_history,
                "terminal_result": "COMPLETE",
            },
            "serial_tool_calls": {
                "parallel_tool_calls": False,
                "provider_call_order": [
                    "call-serial-first",
                    "call-serial-second",
                ],
                "tool_result_order": serial_result_ids[-2:],
            },
            "blank_content_with_tool_calls": {"normalized_to_absent": True},
            "blank_content_without_tool_calls": {"refused": True},
        }
        assert network_events == []
        retained = {
            "fake_transport_calls": complete_transport.request_count,
            "network_probe_events": len(network_events),
            "provider_local_result": complete.terminal_intent.terminal_result,
        }
        serialized = json.dumps(
            {"retained": retained, "release_evidence": release_evidence},
            sort_keys=True,
        )
        assert _RAW_SECRET not in serialized
        assert str(root) not in serialized
        assert "millrace-agents" not in serialized
        assert not any(marker in serialized for marker in _TRANSCRIPT_MARKERS)
        return retained, release_evidence


def main() -> None:
    expected_version, expected_requires_python = sys.argv[1:3]
    extra_arguments = sys.argv[3:]
    assert extra_arguments in ([], ["--release-evidence"])
    distribution = importlib.metadata.distribution("millforge")

    assert distribution.version == expected_version
    assert distribution.metadata["Requires-Python"] == expected_requires_python
    assert expected_requires_python == ">=3.11"
    assert millforge.__version__ == distribution.version
    assert millforge.describe_millforge_base().package_version == distribution.version
    assert Path(millforge.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())

    retained, release_evidence = asyncio.run(_run_public_live_facade())
    retained.update(
        {
            "construction_surface": "millforge.create_millforge_base_live_runner",
            "requires_python": expected_requires_python,
            "version": expected_version,
        }
    )
    if extra_arguments:
        retained["release_evidence"] = release_evidence
    print(json.dumps(retained, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
