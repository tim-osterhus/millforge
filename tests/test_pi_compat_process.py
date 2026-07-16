"""Parity coverage for Pi-compatible shell execution."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import pytest

from millforge.tools.pi_compat import process as process_module
from millforge.tools.pi_compat.contracts import (
    PiCompatErrorKind,
    PiCompatSideEffectState,
)
from millforge.tools.pi_compat.process import (
    PiCompatShellConfig,
    execute_bash,
    resolve_pi_compat_shell,
)


class _Cancellation:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    def cancel(self) -> None:
        self._event.set()


P = ParamSpec("P")
T = TypeVar("T")


def _asyncio_test(function: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, T]:
    """Run an async test without requiring pytest-asyncio in every dev shell."""

    @wraps(function)
    def run(*args: P.args, **kwargs: P.kwargs) -> T:
        return asyncio.run(function(*args, **kwargs))

    return run


@pytest.fixture
def shell_config() -> PiCompatShellConfig:
    return resolve_pi_compat_shell()


def _ordered_output_command() -> str:
    if os.name == "nt":
        return (
            "echo stdout-first & ping -n 2 127.0.0.1 >NUL & "
            "echo stderr-second 1>&2 & ping -n 2 127.0.0.1 >NUL & echo stdout-third"
        )
    return "printf 'stdout-first\\n'; sleep 0.05; printf 'stderr-second\\n' >&2; sleep 0.05; printf 'stdout-third\\n'"


def _long_running_command() -> str:
    return "ping -n 10 127.0.0.1 >NUL" if os.name == "nt" else "sleep 10"


def _many_lines_command() -> str:
    if os.name == "nt":
        # cmd.exe /c uses a single percent sign for a FOR variable.
        return "for /L %i in (1,1,2001) do @echo line-%i"
    return "i=1; while [ $i -le 2001 ]; do printf 'line-%s\\n' \"$i\"; i=$((i+1)); done"


def _burst_order_command() -> str:
    if os.name == "nt":
        return (
            "(for /L %i in (1,1,64) do @echo burst-%i) & "
            "echo stderr-marker 1>&2 & echo stdout-marker"
        )
    return (
        "i=1; while [ $i -le 64 ]; do printf 'burst-%s\\n' \"$i\"; "
        "i=$((i+1)); done; printf 'stderr-marker\\n' >&2; "
        "printf 'stdout-marker\\n'"
    )


async def _wait_for_file(path: Path) -> None:
    for _ in range(100):
        if path.is_file():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


async def _wait_for_pid_exit(pid: int) -> None:
    for _ in range(100):
        if not await asyncio.to_thread(_pid_is_running, pid):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"process {pid} is still running")


def _pid_is_running(pid: int) -> bool:
    if os.name == "nt":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            check=False,
            text=True,
        )
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _owned_tasks() -> list[asyncio.Task[Any]]:
    return [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name().startswith("millforge-pi-bash")
    ]


def test_shell_resolution_uses_host_native_arguments(
    shell_config: PiCompatShellConfig,
) -> None:
    """Spec 11 Section 6: shell discovery is done once before a tool call."""

    executable = Path(shell_config.executable)
    assert executable.is_absolute()
    assert executable.is_file()
    if os.name == "nt":
        # Intentional Spec 11 adaptation from Pi's Windows Bash dependency.
        assert shell_config.arguments == ("/d", "/s", "/c")
    else:
        assert shell_config.arguments == ("-c",)


@_asyncio_test
async def test_execute_bash_keeps_callback_arrival_output_order(
    tmp_path: Path, shell_config: PiCompatShellConfig
) -> None:
    # Pi 0.79.6: packages/coding-agent/test/tools.test.ts
    # "should execute bash commands"
    result = await execute_bash(
        cwd=tmp_path,
        command=_ordered_output_command(),
        timeout_seconds=10.0,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is None
    assert result.exit_code == 0
    assert result.side_effect_state is PiCompatSideEffectState.CONFIRMED_COMPLETE
    assert result.model_text.index("stdout-first") < result.model_text.index(
        "stderr-second"
    )
    assert result.model_text.index("stderr-second") < result.model_text.index(
        "stdout-third"
    )


@_asyncio_test
async def test_execute_bash_keeps_bursty_merged_output_order_without_sleeps(
    tmp_path: Path, shell_config: PiCompatShellConfig
) -> None:
    """One merged pipe must preserve the kernel's write order under a burst."""

    result = await execute_bash(
        cwd=tmp_path,
        command=_burst_order_command(),
        timeout_seconds=10.0,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is None
    assert result.model_text.index("burst-64") < result.model_text.index(
        "stderr-marker"
    )
    assert result.model_text.index("stderr-marker") < result.model_text.index(
        "stdout-marker"
    )


@_asyncio_test
async def test_execute_bash_keeps_event_loop_schedulable(
    tmp_path: Path, shell_config: PiCompatShellConfig
) -> None:
    """The async subprocess path must not block unrelated scheduled work."""

    ticked = asyncio.Event()

    async def tick() -> None:
        await asyncio.sleep(0.01)
        ticked.set()

    tick_task = asyncio.create_task(tick())
    command_task = asyncio.create_task(
        execute_bash(
            cwd=tmp_path,
            command="ping -n 2 127.0.0.1 >NUL" if os.name == "nt" else "sleep 0.1",
            timeout_seconds=10.0,
            cancellation=_Cancellation(),
            shell_config=shell_config,
        )
    )
    try:
        await asyncio.wait_for(ticked.wait(), timeout=1.0)
        assert (await command_task).error_kind is None
    finally:
        await tick_task
        if not command_task.done():
            command_task.cancel()
            await asyncio.gather(command_task, return_exceptions=True)


@pytest.mark.skipif(
    os.name == "nt",
    reason="requires POSIX background-shell process groups and inherited pipe handles",
)
@_asyncio_test
async def test_execute_bash_keeps_late_post_exit_output(
    tmp_path: Path, shell_config: PiCompatShellConfig
) -> None:
    # Pi 0.79.6: packages/coding-agent/test/suite/regressions/5208-late-bash-output.test.ts
    # "captures output emitted after exit while a detached child holds stdout open"
    command = "printf 'HEAD\\n'; ( for i in 1 2 3 4 5 6; do sleep 0.05; printf 'TICK%s\\n' \"$i\"; done ) &"
    result = await execute_bash(
        cwd=tmp_path,
        command=command,
        timeout_seconds=10.0,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is None
    assert "HEAD" in result.model_text
    assert "TICK6" in result.model_text


@pytest.mark.skipif(
    os.name == "nt",
    reason="requires POSIX background-shell process groups and inherited pipe handles",
)
@_asyncio_test
async def test_execute_bash_releases_quiet_inherited_pipe_after_idle_window(
    tmp_path: Path, shell_config: PiCompatShellConfig
) -> None:
    # Pi 0.79.6: packages/coding-agent/test/suite/regressions/5303-bash-output-truncation.test.ts
    # "resolves promptly when a detached child holds stdout open but stays quiet"
    child_pid_path = tmp_path / "quiet-child.pid"
    command = f"printf 'DONE\\n'; sleep 30 & printf '%s' \"$!\" > {shlex.quote(str(child_pid_path))}"
    started = asyncio.get_running_loop().time()
    try:
        result = await execute_bash(
            cwd=tmp_path,
            command=command,
            timeout_seconds=10.0,
            cancellation=_Cancellation(),
            shell_config=shell_config,
        )
        elapsed = asyncio.get_running_loop().time() - started

        assert result.error_kind is None
        assert "DONE" in result.model_text
        assert elapsed < 2.0
    finally:
        if child_pid_path.is_file():
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@_asyncio_test
async def test_execute_bash_tail_truncates_and_persists_full_output(
    tmp_path: Path, shell_config: PiCompatShellConfig
) -> None:
    # Pi 0.79.6: packages/coding-agent/test/tools.test.ts
    # "should truncate bash output to the last 2000 lines"
    result = await execute_bash(
        cwd=tmp_path,
        command=_many_lines_command(),
        timeout_seconds=10.0,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is None
    assert result.truncated is True
    assert result.model_text.startswith("line-2")
    assert "line-2001" in result.model_text
    match = re.search(r"Full output: (.+)]", result.model_text)
    assert match is not None
    full_output = Path(match.group(1))
    try:
        assert full_output.is_file()
        assert "line-1" in full_output.read_text(encoding="utf-8")
        assert "line-2001" in full_output.read_text(encoding="utf-8")
    finally:
        full_output.unlink(missing_ok=True)


@_asyncio_test
async def test_execute_bash_maps_nonzero_exit(
    tmp_path: Path, shell_config: PiCompatShellConfig
) -> None:
    # Pi 0.79.6: packages/coding-agent/test/tools.test.ts
    # "should report command exit errors"
    command = (
        "echo issue & exit /b 7" if os.name == "nt" else "printf 'issue\\n'; exit 7"
    )
    result = await execute_bash(
        cwd=tmp_path,
        command=command,
        timeout_seconds=10.0,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is PiCompatErrorKind.PROCESS_EXIT_NONZERO
    assert result.exit_code == 7
    assert result.side_effect_state is PiCompatSideEffectState.CONFIRMED_COMPLETE
    assert result.model_text.endswith("Command exited with code 7")


@_asyncio_test
async def test_execute_bash_maps_shell_launch_failure(tmp_path: Path) -> None:
    result = await execute_bash(
        cwd=tmp_path,
        command="echo never-runs",
        timeout_seconds=10.0,
        cancellation=_Cancellation(),
        shell_config=PiCompatShellConfig(
            executable=str(tmp_path / "missing-shell"), arguments=()
        ),
    )

    assert result.error_kind is PiCompatErrorKind.PROCESS_LAUNCH_ERROR
    assert result.side_effect_state is PiCompatSideEffectState.NOT_ATTEMPTED


@_asyncio_test
async def test_execute_bash_maps_timeout_and_leaves_no_owned_tasks(
    tmp_path: Path, shell_config: PiCompatShellConfig
) -> None:
    result = await execute_bash(
        cwd=tmp_path,
        command=_long_running_command(),
        timeout_seconds=0.05,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is PiCompatErrorKind.PROCESS_TIMEOUT
    assert result.side_effect_state is PiCompatSideEffectState.COMPLETION_UNKNOWN
    await asyncio.sleep(0)
    assert _owned_tasks() == []


@_asyncio_test
async def test_execute_bash_maps_cancellation(
    tmp_path: Path, shell_config: PiCompatShellConfig
) -> None:
    cancellation = _Cancellation()
    execution = asyncio.create_task(
        execute_bash(
            cwd=tmp_path,
            command=_long_running_command(),
            timeout_seconds=10.0,
            cancellation=cancellation,
            shell_config=shell_config,
        )
    )
    await asyncio.sleep(0.05)
    cancellation.cancel()
    result = await execution

    assert result.error_kind is PiCompatErrorKind.CANCELLED
    assert result.side_effect_state is PiCompatSideEffectState.COMPLETION_UNKNOWN


@_asyncio_test
async def test_execute_bash_timeout_covers_delayed_launch_and_reaps_late_child(
    tmp_path: Path, shell_config: PiCompatShellConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated delayed handoffs are reaped by the parent-owned cleanup task."""

    original_create_process = process_module._create_process
    created_pids: list[int] = []

    async def delayed_create_process(**kwargs: Any) -> asyncio.subprocess.Process:
        await asyncio.sleep(0.03)
        process = await original_create_process(**kwargs)
        assert process.pid is not None
        created_pids.append(process.pid)
        return process

    monkeypatch.setattr(process_module, "_create_process", delayed_create_process)

    for _ in range(4):
        result = await execute_bash(
            cwd=tmp_path,
            command=_long_running_command(),
            timeout_seconds=0.01,
            cancellation=_Cancellation(),
            shell_config=shell_config,
        )

        assert result.error_kind is PiCompatErrorKind.PROCESS_TIMEOUT
        assert result.side_effect_state is PiCompatSideEffectState.COMPLETION_UNKNOWN

    assert len(created_pids) == 4
    for pid in created_pids:
        await _wait_for_pid_exit(pid)
    await asyncio.sleep(0)
    assert _owned_tasks() == []


@_asyncio_test
async def test_execute_bash_timeout_keeps_its_winner_when_launch_join_interrupts(
    tmp_path: Path, shell_config: PiCompatShellConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def interrupted_launch(**_kwargs: Any) -> asyncio.subprocess.Process:
        await asyncio.sleep(0.03)
        raise process_module._LaunchInterrupted("launch handoff interrupted")

    monkeypatch.setattr(process_module, "_launch_process", interrupted_launch)
    result = await execute_bash(
        cwd=tmp_path,
        command=_long_running_command(),
        timeout_seconds=0.01,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is PiCompatErrorKind.PROCESS_TIMEOUT
    assert result.side_effect_state is PiCompatSideEffectState.COMPLETION_UNKNOWN
    assert _owned_tasks() == []


@pytest.mark.skipif(
    os.name == "nt",
    reason="requires POSIX killpg process-group primitive",
)
@_asyncio_test
async def test_execute_bash_tree_kill_failure_uses_direct_child_fallback(
    tmp_path: Path, shell_config: PiCompatShellConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed group kill must still bound cleanup and kill the direct shell."""

    pid_path = tmp_path / "shell.pid"

    def unavailable_killpg(*_args: Any) -> None:
        raise OSError("killpg unavailable")

    monkeypatch.setattr(process_module.os, "killpg", unavailable_killpg)
    result = await execute_bash(
        cwd=tmp_path,
        command=f"printf '%s' \"$$\" > {shlex.quote(str(pid_path))}; exec sleep 10",
        timeout_seconds=0.2,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    await _wait_for_file(pid_path)
    await _wait_for_pid_exit(int(pid_path.read_text(encoding="utf-8")))
    assert result.error_kind is PiCompatErrorKind.PROCESS_TIMEOUT
    assert result.side_effect_state is PiCompatSideEffectState.COMPLETION_UNKNOWN
    assert _owned_tasks() == []


@pytest.mark.skipif(
    os.name != "nt",
    reason="requires the Windows taskkill.exe process-tree primitive (unavailable under WSL)",
)
@_asyncio_test
async def test_execute_bash_taskkill_failure_uses_direct_child_fallback(
    tmp_path: Path, shell_config: PiCompatShellConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows taskkill failure still contains the direct cmd.exe child."""

    async def failed_taskkill(_pid: int) -> bool:
        return False

    monkeypatch.setattr(process_module, "_run_taskkill", failed_taskkill)
    result = await execute_bash(
        cwd=tmp_path,
        command=_long_running_command(),
        timeout_seconds=0.05,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is PiCompatErrorKind.PROCESS_TIMEOUT
    assert result.side_effect_state is PiCompatSideEffectState.COMPLETION_UNKNOWN
    assert _owned_tasks() == []


@pytest.mark.skipif(
    os.name == "nt",
    reason="requires POSIX shell PID and killpg process-group primitives",
)
@_asyncio_test
async def test_caller_task_cancellation_reaps_the_direct_os_process(
    tmp_path: Path, shell_config: PiCompatShellConfig
) -> None:
    """External Task.cancel follows the same owned cleanup path as token cancel."""

    pid_path = tmp_path / "shell.pid"
    execution = asyncio.create_task(
        execute_bash(
            cwd=tmp_path,
            command=f"printf '%s' \"$$\" > {shlex.quote(str(pid_path))}; exec sleep 10",
            timeout_seconds=10.0,
            cancellation=_Cancellation(),
            shell_config=shell_config,
        )
    )
    await _wait_for_file(pid_path)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    await _wait_for_pid_exit(int(pid_path.read_text(encoding="utf-8")))
    assert _owned_tasks() == []


@_asyncio_test
async def test_execute_bash_maps_reader_failure_before_exit_to_unknown_io_error(
    tmp_path: Path, shell_config: PiCompatShellConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed_reader(
        _stream: asyncio.StreamReader,
        _accumulator: Any,
        _activity: asyncio.Event,
    ) -> None:
        raise OSError("reader exploded")

    monkeypatch.setattr(process_module, "_read_output", failed_reader)
    result = await execute_bash(
        cwd=tmp_path,
        command=_long_running_command(),
        timeout_seconds=10.0,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is PiCompatErrorKind.IO_ERROR
    assert result.side_effect_state is PiCompatSideEffectState.COMPLETION_UNKNOWN
    assert "reader exploded" in result.model_text
    assert _owned_tasks() == []


@_asyncio_test
async def test_execute_bash_maps_reader_failure_after_exit_to_complete_io_error(
    tmp_path: Path, shell_config: PiCompatShellConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def delayed_failed_reader(
        _stream: asyncio.StreamReader,
        _accumulator: Any,
        _activity: asyncio.Event,
    ) -> None:
        await asyncio.sleep(0.05)
        raise OSError("reader exploded after exit")

    monkeypatch.setattr(process_module, "_read_output", delayed_failed_reader)
    monkeypatch.setattr(process_module, "_exit_is_established", lambda _task: False)
    result = await execute_bash(
        cwd=tmp_path,
        command="echo complete-before-reader-error",
        timeout_seconds=10.0,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is PiCompatErrorKind.IO_ERROR
    assert result.side_effect_state is PiCompatSideEffectState.CONFIRMED_COMPLETE
    assert "reader exploded after exit" in result.model_text
    assert _owned_tasks() == []


@_asyncio_test
async def test_execute_bash_maps_capture_failure_and_removes_partial_output(
    tmp_path: Path, shell_config: PiCompatShellConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    partial_output = tmp_path / "partial-output.log"

    def capture_path() -> Path:
        return partial_output

    def failed_write(_output_file: Any, _data: bytes) -> None:
        time.sleep(0.05)
        raise OSError("capture write failed")

    monkeypatch.setattr(process_module, "_create_raw_output_path", capture_path)
    monkeypatch.setattr(process_module, "_write_raw_chunk", failed_write)
    result = await execute_bash(
        cwd=tmp_path,
        command="echo capture-data",
        timeout_seconds=10.0,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is PiCompatErrorKind.IO_ERROR
    assert result.side_effect_state is PiCompatSideEffectState.CONFIRMED_COMPLETE
    assert "capture write failed" in result.model_text
    assert partial_output.exists() is False
    assert _owned_tasks() == []


@_asyncio_test
async def test_execute_bash_maps_capture_failure_before_exit_to_unknown_io_error(
    tmp_path: Path, shell_config: PiCompatShellConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_write(_output_file: Any, _data: bytes) -> None:
        raise OSError("capture write failed before exit")

    monkeypatch.setattr(process_module, "_write_raw_chunk", failed_write)
    command = (
        "echo capture-before-exit & ping -n 10 127.0.0.1 >NUL"
        if os.name == "nt"
        else "printf 'capture-before-exit\\n'; sleep 10"
    )
    result = await execute_bash(
        cwd=tmp_path,
        command=command,
        timeout_seconds=10.0,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is PiCompatErrorKind.IO_ERROR
    assert result.side_effect_state is PiCompatSideEffectState.COMPLETION_UNKNOWN
    assert "capture write failed before exit" in result.model_text
    assert _owned_tasks() == []


@_asyncio_test
async def test_caller_cancellation_waits_for_writer_before_removing_output(
    tmp_path: Path, shell_config: PiCompatShellConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thread-bound write cannot outlive the file handle that owns it."""

    raw_output = tmp_path / "owned-output.log"
    write_started = threading.Event()
    release_write = threading.Event()
    original_write = process_module._write_raw_chunk

    def capture_path() -> Path:
        return raw_output

    def blocked_write(output_file: Any, data: bytes) -> None:
        write_started.set()
        release_write.wait(timeout=2.0)
        original_write(output_file, data)

    monkeypatch.setattr(process_module, "_create_raw_output_path", capture_path)
    monkeypatch.setattr(process_module, "_write_raw_chunk", blocked_write)
    execution = asyncio.create_task(
        execute_bash(
            cwd=tmp_path,
            command=_many_lines_command(),
            timeout_seconds=10.0,
            cancellation=_Cancellation(),
            shell_config=shell_config,
        )
    )
    try:
        await asyncio.wait_for(asyncio.to_thread(write_started.wait), timeout=1.0)
        execution.cancel()
        await asyncio.sleep(0.05)
        assert execution.done() is False
    finally:
        release_write.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=2.0)
    assert raw_output.exists() is False
    assert _owned_tasks() == []


@pytest.mark.parametrize(
    ("timeout_seconds", "expected"),
    [
        (5.0, "5"),
        (0.5, "0.5"),
        (1e-6, "0.000001"),
        (1e-7, "1e-7"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (1.23e-7, "1.23e-7"),
        (1.23e20, "123000000000000000000"),
    ],
)
def test_timeout_messages_match_javascript_number_formatting(
    timeout_seconds: float, expected: str
) -> None:
    assert process_module._format_timeout_seconds(timeout_seconds) == expected


@pytest.mark.skipif(
    os.name != "nt",
    reason="requires Windows cmd.exe and taskkill.exe primitives (unavailable under WSL)",
)
@_asyncio_test
async def test_cmd_inherited_handle_releases_after_idle_window(
    tmp_path: Path, shell_config: PiCompatShellConfig
) -> None:
    # Pi 0.79.6: packages/coding-agent/test/bash-close-hang-windows.test.ts
    # "handles delayed close events without hanging"
    started = asyncio.get_running_loop().time()
    result = await execute_bash(
        cwd=tmp_path,
        command=(
            "powershell.exe -NoProfile -Command "
            '"$info = New-Object System.Diagnostics.ProcessStartInfo; '
            "$info.FileName = 'powershell.exe'; "
            "$info.Arguments = '-NoProfile -Command Start-Sleep -Seconds 1'; "
            "$info.UseShellExecute = $false; "
            "$null = [System.Diagnostics.Process]::Start($info); "
            "Write-Output 'DONE'" + '"'
        ),
        timeout_seconds=10.0,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is None
    assert "DONE" in result.model_text
    assert asyncio.get_running_loop().time() - started < 2.0


@pytest.mark.skipif(
    os.name == "nt",
    reason="requires POSIX process-group semantics to observe descendant cleanup",
)
@_asyncio_test
async def test_timeout_kills_posix_process_group(
    tmp_path: Path, shell_config: PiCompatShellConfig
) -> None:
    """Timeout must kill the shell's entire new session, not only its PID."""

    marker = tmp_path / "descendant-ran.txt"
    ready = tmp_path / "descendant-ready.txt"
    child_pid_path = tmp_path / "descendant.pid"
    command = (
        f"( printf ready > {shlex.quote(str(ready))}; sleep 2; "
        f"printf child > {shlex.quote(str(marker))} ) & "
        f"printf '%s' \"$!\" > {shlex.quote(str(child_pid_path))}; sleep 10"
    )
    execution = asyncio.create_task(
        execute_bash(
            cwd=tmp_path,
            command=command,
            timeout_seconds=0.75,
            cancellation=_Cancellation(),
            shell_config=shell_config,
        )
    )
    try:
        for _ in range(50):
            if ready.exists():
                break
            await asyncio.sleep(0.01)
        assert ready.exists()
        result = await execution
        await asyncio.sleep(0.1)

        assert result.error_kind is PiCompatErrorKind.PROCESS_TIMEOUT
        assert marker.exists() is False
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        if child_pid_path.is_file():
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            try:
                os.killpg(os.getpgid(child_pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("timeout_seconds", [0.0, -1.0, float("inf"), float("nan")])
@_asyncio_test
async def test_execute_bash_rejects_nonfinite_or_nonpositive_timeout(
    tmp_path: Path, shell_config: PiCompatShellConfig, timeout_seconds: float
) -> None:
    result = await execute_bash(
        cwd=tmp_path,
        command="echo never-runs",
        timeout_seconds=timeout_seconds,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is PiCompatErrorKind.INVALID_ARGUMENTS
    assert result.side_effect_state is PiCompatSideEffectState.NOT_ATTEMPTED


@_asyncio_test
async def test_execute_bash_rejects_relative_cwd(
    shell_config: PiCompatShellConfig,
) -> None:
    result = await execute_bash(
        cwd=Path("relative"),
        command="echo never-runs",
        timeout_seconds=1.0,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is PiCompatErrorKind.INVALID_ARGUMENTS
    assert result.side_effect_state is PiCompatSideEffectState.NOT_ATTEMPTED


@_asyncio_test
async def test_execute_bash_rejects_nul_cwd_without_echoing_the_nul(
    tmp_path: Path, shell_config: PiCompatShellConfig
) -> None:
    result = await execute_bash(
        cwd=Path(f"{tmp_path}\x00suffix"),
        command="echo never-runs",
        timeout_seconds=1.0,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is PiCompatErrorKind.INVALID_ARGUMENTS
    assert result.side_effect_state is PiCompatSideEffectState.NOT_ATTEMPTED
    assert "\x00" not in result.model_text


@_asyncio_test
async def test_execute_bash_rejects_filesystem_unencodable_cwd(
    tmp_path: Path, shell_config: PiCompatShellConfig
) -> None:
    cwd = Path(f"{tmp_path}\ud800")
    with pytest.raises(UnicodeEncodeError):
        str(cwd).encode(sys.getfilesystemencoding(), "strict")

    result = await execute_bash(
        cwd=cwd,
        command="echo never-runs",
        timeout_seconds=1.0,
        cancellation=_Cancellation(),
        shell_config=shell_config,
    )

    assert result.error_kind is PiCompatErrorKind.INVALID_ARGUMENTS
    assert result.side_effect_state is PiCompatSideEffectState.NOT_ATTEMPTED
