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
    HarnessTaskInput,
    MillforgeBaseLiveRunner,
    MillforgeBaseOptions,
    ModelProfileRef,
    OpenAICompatibleTimeouts,
    RequestOptionAllowlist,
    ResolvedModelProfile,
    ResolvedSecret,
    RunDirRef,
    SecretRef,
    StageIdentity,
    TimeoutRef,
    create_millforge_base_live_runner,
)


_RAW_SECRET = "package-smoke-secret-value-7e4f0d"
_TRANSCRIPT_MARKERS = (
    "Read the package note.",
    "Installed package traversal complete.",
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
            }
        ),
        request_options=RequestOptionAllowlist(
            allowed_options=("parallel_tool_calls",),
        ),
        timeout_seconds=30,
        source_digest="installed-package-smoke-v2",
    )


class _FakeOpenAITransport:
    """Caller-owned transport that records counts, never request transcripts."""

    def __init__(self) -> None:
        self.request_count = 0
        self.close_calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        body = json.loads(request.content)
        assert body["model"] == "fake-tool-model"
        assert body["parallel_tool_calls"] is False
        assert request.url == "https://models.invalid/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {_RAW_SECRET}"

        if self.request_count == 1:
            message = {
                "role": "assistant",
                "content": _TRANSCRIPT_MARKERS[0],
                "tool_calls": [
                    {
                        "id": "call-read",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"path":"note.txt"}',
                        },
                    }
                ],
            }
        elif self.request_count == 2:
            message = {
                "role": "assistant",
                "content": _TRANSCRIPT_MARKERS[1],
                "tool_calls": [
                    {
                        "id": "call-submit",
                        "type": "function",
                        "function": {
                            "name": "submit",
                            "arguments": (
                                '{"summary":"offline installed smoke complete",'
                                '"terminal_result":"COMPLETE"}'
                            ),
                        },
                    }
                ],
            }
        else:
            raise AssertionError("unexpected additional model request")

        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "model": "fake-tool-model",
                "choices": [{"finish_reason": "tool_calls", "message": message}],
            },
            request=request,
        )

    async def aclose(self) -> None:
        self.close_calls += 1


def _request(
    live_runner: MillforgeBaseLiveRunner, root: Path, secret_ref: SecretRef
) -> HarnessExecutionRequest:
    plan = live_runner.components.compiled_plan
    run_id = "run-installed-package-smoke"
    return HarnessExecutionRequest(
        request_id="request-installed-package-smoke",
        run_id=run_id,
        work_item_id="work-installed-package-smoke",
        task=HarnessTaskInput(instruction="Read note.txt and complete."),
        stage=StageIdentity(
            plane="execution",
            node_id="millforge-base",
            stage_kind_id="millforge_base",
        ),
        compiled_harness=CompiledHarnessRef(
            identity=CompiledHarnessIdentity(
                compiled_plan_id="plan-installed-package-smoke",
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
        cancellation=CancellationRef(cancellation_id="cancel-installed-smoke"),
        secret_refs=(secret_ref,),
        model_profile=ModelProfileRef(profile_id=plan.model_profile.profile_id),
    )


async def _run_public_live_facade() -> dict[str, object]:
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
        secret_ref = SecretRef(
            secret_id="package-smoke-secret",
            env_var="MILLFORGE_PACKAGE_SMOKE_SECRET",
        )
        resolver = _CancellationResolver()
        secret_resolver = _SecretResolver(secret_ref)
        transport = _FakeOpenAITransport()

        live_runner = await create_millforge_base_live_runner(
            profile_id="package-smoke-openai-compatible",
            model_profile=_model_profile(secret_ref),
            secret_ref=secret_ref,
            secret_resolver=secret_resolver,
            cwd=root,
            clock=_Clock(),
            cancellation_resolver=resolver,
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
        assert transport.request_count == 0
        assert secret_resolver.resolve_calls == 0

        async with live_runner:
            result = await live_runner.execute(_request(live_runner, root, secret_ref))

        assert result.status is ExecutionStatus.COMPLETED
        assert result.terminal_intent is not None
        assert result.terminal_intent.terminal_result == "COMPLETE"
        assert transport.request_count == 2
        assert transport.close_calls == 0
        assert secret_resolver.resolve_calls == 2
        assert network_events == []
        await transport.aclose()
        assert transport.close_calls == 1

        retained = {
            "fake_transport_calls": transport.request_count,
            "network_probe_events": len(network_events),
            "provider_local_result": result.terminal_intent.terminal_result,
        }
        serialized = json.dumps(retained, sort_keys=True)
        assert _RAW_SECRET not in serialized
        assert str(root) not in serialized
        assert "millrace-agents" not in serialized
        assert not any(marker in serialized for marker in _TRANSCRIPT_MARKERS)
        return retained


def main() -> None:
    expected_version, expected_requires_python = sys.argv[1:3]
    distribution = importlib.metadata.distribution("millforge")

    assert distribution.version == expected_version
    assert distribution.metadata["Requires-Python"] == expected_requires_python
    assert expected_requires_python == ">=3.11"
    assert millforge.__version__ == distribution.version
    assert millforge.describe_millforge_base().package_version == distribution.version
    assert Path(millforge.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())

    retained = asyncio.run(_run_public_live_facade())
    retained.update(
        {
            "construction_surface": "millforge.create_millforge_base_live_runner",
            "requires_python": expected_requires_python,
            "version": expected_version,
        }
    )
    print(json.dumps(retained, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
