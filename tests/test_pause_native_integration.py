"""Actual native runtime/loop/tools; only external HTTP responses are scripted."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from millforge import (
    EndpointConfig,
    MillforgeBaseOptions,
    OpenAICompatibleTimeouts,
    create_millforge_base_live_runner,
)
from millforge.pause_control import (
    NativeClock,
    NativeCancellationToken,
    NativeCancellationResolver,
    NativeEnvironmentSecretResolver,
    PROFILE,
    PROFILE_DIGEST,
)
from tests.test_base_runner import _live_model_profile, _request

P = {**PROFILE, "profile_digest": PROFILE_DIGEST}


class ModelServer:
    def __init__(self, delay=0.15):
        self.count = 0
        self.slow_started = threading.Event()
        self.delay = delay
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                owner.count += 1
                index = owner.count
                if index == 3:
                    owner.slow_started.set()
                    time.sleep(owner.delay)
                name, args = [
                    ("write", {"path": "before.txt", "content": "before"}),
                    ("read", {"path": "before.txt"}),
                    ("write", {"path": "after.txt", "content": "after"}),
                    ("submit", {"summary": "done", "terminal_result": "COMPLETE"}),
                ][index - 1]
                body = {
                    "id": f"response-{index}",
                    "model": owner.model,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": f"call-{index}",
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": json.dumps(args),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                    },
                }
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(2)


async def live(tmp_path, monkeypatch, server):
    profile = _live_model_profile().model_copy(
        update={
            "endpoint": EndpointConfig(
                base_url=f"http://127.0.0.1:{server.http.server_port}/v1",
                allow_insecure_local=True,
            )
        }
    )
    server.model = profile.model_id
    secret = profile.authentication.secret_ref
    monkeypatch.setenv(secret.env_var, "local-script-only")
    token = NativeCancellationToken("cancel-one")
    facade = await create_millforge_base_live_runner(
        profile_id=profile.profile_id,
        model_profile=profile,
        secret_ref=secret,
        secret_resolver=NativeEnvironmentSecretResolver(secret),
        cwd=tmp_path,
        clock=NativeClock(),
        cancellation_resolver=NativeCancellationResolver(token),
        timeouts=OpenAICompatibleTimeouts.uniform(30),
        options=MillforgeBaseOptions(load_context_files=False),
    )
    request = _request(facade.components, tmp_path / "artifacts").model_copy(
        update={"secret_refs": (secret,)}
    )
    facade.native_control.set_journal(lambda _: None)
    return facade, request, token


async def until(predicate, seconds=10):
    end = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < end
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [0.15, 6.25])
async def test_actual_native_started_hold_same_invocation(tmp_path, monkeypatch, delay):
    server = ModelServer(delay)
    facade, request, token = await live(tmp_path, monkeypatch, server)
    control = facade.native_control
    task = asyncio.create_task(facade.execute(request))
    try:
        await until(lambda: server.slow_started.is_set() or task.done())
        assert not task.done(), task.result()
        assert (tmp_path / "before.txt").read_text() == "before"
        before = control.snapshot()
        with control.reservation(P, "runs.pause", None):
            control.accept("pause", "d1", "runs.pause", "pause-id")
        if delay > 5:
            await asyncio.sleep(5)
            assert control.snapshot()["state"] == "pause_pending"
        await until(lambda: control.snapshot()["state"] == "held")
        held = control.snapshot()
        assert held["active_effects"] == 0 and held["eligible"]
        assert not (tmp_path / "after.txt").exists()
        await asyncio.sleep(0.2)
        assert control.snapshot() == held and server.count == 3
        with control.reservation(P, "runs.resume", "pause-id"):
            control.accept("resume", "d2", "runs.resume", "pause-id")
        control.confirm_settlement("resume")
        result = await asyncio.wait_for(task, 10)
        assert result.status.value == "completed", result
        assert (tmp_path / "after.txt").read_text() == "after"
        assert control.snapshot()["session_id"] == before["session_id"]
        assert control.snapshot()["invocation_id"] == before["invocation_id"]
        assert server.count == 4
    finally:
        if not task.done():
            token.cancel("test cleanup")
            await asyncio.wait_for(task, 10)
        await facade.aclose()
        server.close()


@pytest.mark.asyncio
async def test_injected_clock_cannot_qualify_actual_facade(tmp_path, monkeypatch):
    profile = _live_model_profile()
    secret = profile.authentication.secret_ref
    monkeypatch.setenv(secret.env_var, "local-script-only")
    token = NativeCancellationToken("cancel-injected")

    class LookalikeClock(NativeClock):
        pass

    facade = await create_millforge_base_live_runner(
        profile_id=profile.profile_id,
        model_profile=profile,
        secret_ref=secret,
        secret_resolver=NativeEnvironmentSecretResolver(secret),
        cwd=tmp_path,
        clock=LookalikeClock(),
        cancellation_resolver=NativeCancellationResolver(token),
        timeouts=OpenAICompatibleTimeouts.uniform(30),
        options=MillforgeBaseOptions(load_context_files=False),
    )
    try:
        assert not facade.native_control.snapshot()["eligible"]
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_stock_write_path_removes_qualification_before_symlink_entry(
    tmp_path, monkeypatch
):
    outside = tmp_path.parent / "outside-native-profile.txt"
    outside.write_text("retained outside bytes")
    (tmp_path / "after.txt").symlink_to(outside)
    server = ModelServer()
    facade, request, token = await live(tmp_path, monkeypatch, server)
    try:
        await asyncio.wait_for(facade.execute(request), 10)
        assert (tmp_path / "before.txt").read_text() == "before"
        snapshot = facade.native_control.snapshot()
        assert snapshot["eligible"] is False
        assert snapshot["reason"] == "unsupported_tool"
        assert snapshot["os_descendants"] is None
        with pytest.raises(RuntimeError, match="native_profile_unqualified"):
            with facade.native_control.reservation(P, "runs.pause", None):
                pytest.fail("symlink stock effect retained pause qualification")
    finally:
        await facade.aclose()
        server.close()
