"""Exercise an installed Millforge artifact through its stable base facade."""

from __future__ import annotations

import asyncio
import datetime
import importlib.metadata
import sys
import tempfile
from pathlib import Path

import millforge
from millforge import (
    AssistantMessage,
    CancellationRef,
    CompiledHarnessHash,
    CompiledHarnessIdentity,
    CompiledHarnessRef,
    ExecutionStatus,
    HarnessExecutionRequest,
    HarnessTaskInput,
    MillforgeBaseOptions,
    MillforgeBaseRuntimeServices,
    ModelCompletionResponse,
    ModelProfileRef,
    ModelToolCall,
    ParsedToolArguments,
    RunDirRef,
    StageIdentity,
    TimeoutRef,
    create_millforge_base_components,
    create_millforge_base_runner,
)
from millforge.model_backend import (
    AuthenticationPolicy,
    AuthenticationScheme,
    CapabilityDeclarations,
    CapabilitySupport,
    EndpointConfig,
    ReasoningMode,
    ReasoningPolicy,
    RequestOptionAllowlist,
    ResolvedModelProfile,
    SamplingPolicy,
)
from millforge.testing import FakeModelClient


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
        return datetime.datetime(2026, 7, 17, tzinfo=datetime.timezone.utc)

    def monotonic(self) -> float:
        return 1.0


def _model_profile() -> ResolvedModelProfile:
    return ResolvedModelProfile(
        profile_id="fake.package-smoke.v1",
        provider_id="fake",
        model_id="fake-tools",
        endpoint=EndpointConfig(base_url="https://unused.invalid/v1"),
        authentication=AuthenticationPolicy(scheme=AuthenticationScheme.NONE),
        sampling=SamplingPolicy(
            allowed_overrides=(),
            allow_maximum_output_tokens_override=False,
        ),
        reasoning=ReasoningPolicy(mode=ReasoningMode.DISABLED),
        capabilities=CapabilityDeclarations(
            support={
                "tool_calls": CapabilitySupport.SUPPORTED,
                "system_messages": CapabilitySupport.SUPPORTED,
                "tool_result_messages": CapabilitySupport.SUPPORTED,
                "parallel_tool_calls": CapabilitySupport.UNSUPPORTED,
                "structured_output": CapabilitySupport.UNSUPPORTED,
                "reasoning_controls": CapabilitySupport.UNSUPPORTED,
                "usage_reporting": CapabilitySupport.UNSUPPORTED,
            }
        ),
        request_options=RequestOptionAllowlist(),
        source_digest="package-smoke-v1",
    )


def _response(model_id: str, *, terminal: bool) -> ModelCompletionResponse:
    name = "submit" if terminal else "read"
    arguments = (
        {"terminal_result": "COMPLETE", "summary": "package smoke complete"}
        if terminal
        else {"path": "note.txt"}
    )
    return ModelCompletionResponse(
        provider_request_id=f"fake-{name}",
        model_id=model_id,
        message=AssistantMessage(
            content="deterministic package smoke",
            tool_calls=(
                ModelToolCall(
                    call_id=f"call-{name}",
                    name=name,
                    arguments=ParsedToolArguments(value=arguments),
                ),
            ),
        ),
        finish_reason="tool_calls",
        usage=None,
    )


async def _run_facade() -> None:
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root).resolve()
        (root / "note.txt").write_text("offline package smoke\n", encoding="utf-8")
        resolver = _CancellationResolver()
        components = create_millforge_base_components(
            model_profile=_model_profile(),
            cwd=root,
            home_directory=root,
            cancellation_resolver=resolver,
            options=MillforgeBaseOptions(load_context_files=False),
            prompt_date=datetime.date(2026, 7, 17),
        )
        plan = components.compiled_plan
        model = FakeModelClient(
            responses=[
                _response(plan.model_profile.profile_id, terminal=False),
                _response(plan.model_profile.profile_id, terminal=True),
            ]
        )
        runner = create_millforge_base_runner(
            components=components,
            services=MillforgeBaseRuntimeServices(
                model_client=model,
                clock=_Clock(),
                cancellation_resolver=resolver,
            ),
        )
        request = HarnessExecutionRequest(
            request_id="request-package-smoke-v1",
            run_id="run-package-smoke-v1",
            work_item_id="work-package-smoke-v1",
            task=HarnessTaskInput(instruction="Read note.txt and complete."),
            stage=StageIdentity(
                plane="execution",
                node_id="millforge-base",
                stage_kind_id="millforge_base",
            ),
            compiled_harness=CompiledHarnessRef(
                identity=CompiledHarnessIdentity(
                    compiled_plan_id="plan-package-smoke-v1",
                    harness_id=plan.harness_id,
                    harness_version=plan.harness_version,
                ),
                path=root / "compiled-plan.json",
                expected_hash=CompiledHarnessHash(
                    algorithm="sha256",
                    digest=plan.compiled_sha256,
                ),
            ),
            capability_envelope=components.capability_envelope,
            input_artifacts=(),
            run_directory=RunDirRef(run_id="run-package-smoke-v1", path=root),
            timeout=TimeoutRef(timeout_seconds=60),
            cancellation=CancellationRef(cancellation_id="cancel-package-smoke-v1"),
            secret_refs=(),
            model_profile=ModelProfileRef(profile_id=plan.model_profile.profile_id),
        )

        result = await runner.execute(request)

        assert result.status is ExecutionStatus.COMPLETED
        assert result.terminal_intent is not None
        assert result.terminal_intent.terminal_result == "COMPLETE"
        assert model.call_count == 2
        assert (root / "millforge" / "terminal_result.json").is_file()


def main() -> None:
    expected_version, expected_requires_python = sys.argv[1:3]
    distribution = importlib.metadata.distribution("millforge")

    assert distribution.version == expected_version
    assert distribution.metadata["Requires-Python"] == expected_requires_python
    assert expected_requires_python == ">=3.11"
    assert millforge.__version__ == distribution.version
    assert millforge.describe_millforge_base().package_version == distribution.version
    asyncio.run(_run_facade())


if __name__ == "__main__":
    main()
