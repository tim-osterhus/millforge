"""Pi 0.79.6-compatible local shell execution.

One ``execute_bash`` call owns its launch task, shell process, merged output
stream, raw-output writer, cancellation waits, and cleanup.  In particular,
the post-exit drain is necessary because a shell can exit while a descendant
still holds an inherited output pipe open.
"""

from __future__ import annotations

import asyncio
import codecs
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, TypeVar, runtime_checkable

from .contracts import (
    PiCompatErrorKind,
    PiCompatOperationResult,
    PiCompatSideEffectState,
)
from .paths import _is_absolute_cwd
from .truncation import format_size, truncate_tail

__all__ = [
    "PiCompatCancellation",
    "PiCompatShellConfig",
    "PiCompatShellResolutionError",
    "execute_bash",
    "resolve_pi_compat_shell",
]

_DEFAULT_MAX_LINES = 2_000
_DEFAULT_MAX_BYTES = 50 * 1024
_POST_EXIT_IDLE_SECONDS = 0.1
_PROCESS_POLL_SECONDS = 0.01
_CLEANUP_GRACE_SECONDS = 30.0
_CAPTURE_QUEUE_CHUNKS = 16

_T = TypeVar("_T")


@runtime_checkable
class PiCompatCancellation(Protocol):
    """Minimal cancellation surface supplied by the Millforge adapter."""

    def is_cancelled(self) -> bool: ...

    async def wait(self) -> None: ...


@dataclass(frozen=True)
class PiCompatShellConfig:
    """One resolved host shell and the arguments preceding its command."""

    executable: str
    arguments: tuple[str, ...]


class PiCompatShellResolutionError(RuntimeError):
    """Raised when the host has no usable shell for Pi-compatible bash calls."""


class _LaunchInterrupted(RuntimeError):
    """The process was created after its caller had already interrupted launch."""


@dataclass(frozen=True)
class _TailSnapshot:
    content: str
    truncated: bool
    truncated_by: str | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool
    last_line_bytes: int
    full_output_path: str | None


@dataclass(frozen=True)
class _CleanupOutcome:
    tree_confirmed: bool
    process_exited: bool
    process_reaped: bool
    diagnostics: tuple[str, ...] = ()


class _OutputAccumulator:
    """Bounded Pi-style tail display plus one owned raw-output writer."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._raw_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=_CAPTURE_QUEUE_CHUNKS
        )
        self._writer_task = asyncio.create_task(
            _write_raw_output(self._raw_queue),
            name="millforge-pi-bash-output-writer",
        )
        self._writer_stop_requested = False
        self._raw_output_path: Path | None = None
        self._tail_text = ""
        self._tail_bytes = 0
        self._tail_starts_at_line_boundary = True
        self._total_decoded_bytes = 0
        self._completed_lines = 0
        self._total_lines = 0
        self._current_line_bytes = 0
        self._has_open_line = False
        self._finished = False
        self._snapshot_result: _TailSnapshot | None = None
        self._keep_raw_output = False
        self._cleanup_diagnostics: list[str] = []

    @property
    def writer_task(self) -> asyncio.Task[Path]:
        return self._writer_task

    async def append(self, data: bytes) -> None:
        if self._finished:
            raise RuntimeError("cannot append to a finished output accumulator")
        if not data:
            return
        await self._enqueue_raw_output(data)
        self._append_decoded_text(self._decoder.decode(data, final=False))

    async def finish(self) -> _TailSnapshot:
        if self._snapshot_result is not None:
            return self._snapshot_result
        if not self._finished:
            self._finished = True
            self._append_decoded_text(self._decoder.decode(b"", final=True))

        raw_output_path = await self._finish_writer()
        snapshot = self._snapshot()
        if snapshot.truncated:
            self._keep_raw_output = True
            snapshot = _TailSnapshot(
                content=snapshot.content,
                truncated=snapshot.truncated,
                truncated_by=snapshot.truncated_by,
                total_lines=snapshot.total_lines,
                total_bytes=snapshot.total_bytes,
                output_lines=snapshot.output_lines,
                output_bytes=snapshot.output_bytes,
                last_line_partial=snapshot.last_line_partial,
                last_line_bytes=snapshot.last_line_bytes,
                full_output_path=str(raw_output_path),
            )
        self._snapshot_result = snapshot
        return snapshot

    async def close(self) -> tuple[str, ...]:
        """Finish the writer before removing any unreturned raw-output file."""

        cancellation: asyncio.CancelledError | None = None
        diagnostics: list[str] = []
        try:
            await self._finish_writer()
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception as exc:
            diagnostics.append(_format_cleanup_diagnostic(exc))

        if not self._keep_raw_output and self._raw_output_path is not None:
            raw_output_path = self._raw_output_path
            self._raw_output_path = None
            try:
                await _run_blocking(lambda: raw_output_path.unlink(missing_ok=True))
            except asyncio.CancelledError as exc:
                cancellation = exc
            except OSError as exc:
                diagnostics.append(_format_cleanup_diagnostic(exc))
        self._cleanup_diagnostics.extend(diagnostics)
        if cancellation is not None:
            raise cancellation
        return tuple(diagnostics)

    def snapshot_after_failure(self) -> _TailSnapshot:
        """Return the model tail after an already-observed capture failure."""

        if not self._finished:
            self._finished = True
            self._append_decoded_text(self._decoder.decode(b"", final=True))
        return self._snapshot()

    async def _enqueue_raw_output(self, data: bytes | None) -> None:
        if self._writer_task.done():
            await _observe_task(self._writer_task)
            raise RuntimeError("raw-output writer completed before capture finished")
        try:
            self._raw_queue.put_nowait(data)
            return
        except asyncio.QueueFull:
            pass

        put_task = asyncio.create_task(
            self._raw_queue.put(data), name="millforge-pi-bash-output-queue-put"
        )
        try:
            done, _ = await asyncio.wait(
                {put_task, self._writer_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if self._writer_task in done:
                if not put_task.done():
                    await _cancel_and_observe((put_task,))
                else:
                    await _observe_task(put_task)
                await _observe_task(self._writer_task)
                raise RuntimeError(
                    "raw-output writer completed before capture finished"
                )
            await _observe_task(put_task)
        finally:
            if not put_task.done():
                await _cancel_and_observe((put_task,))

    async def _finish_writer(self) -> Path:
        if not self._writer_stop_requested:
            self._writer_stop_requested = True
            await self._enqueue_raw_output(None)
        raw_output_path = await _await_owned_task(self._writer_task)
        self._raw_output_path = raw_output_path
        return raw_output_path

    def _append_decoded_text(self, text: str) -> None:
        if not text:
            return

        text_bytes = len(text.encode("utf-8"))
        self._total_decoded_bytes += text_bytes
        self._tail_text += text
        self._tail_bytes += text_bytes
        if self._tail_bytes > _DEFAULT_MAX_BYTES * 4:
            self._trim_tail()

        newline_count = text.count("\n")
        if newline_count == 0:
            self._current_line_bytes += text_bytes
            self._has_open_line = True
        else:
            self._completed_lines += newline_count
            trailing_text = text.rsplit("\n", maxsplit=1)[1]
            self._current_line_bytes = len(trailing_text.encode("utf-8"))
            self._has_open_line = bool(trailing_text)
        self._total_lines = self._completed_lines + int(self._has_open_line)

    def _trim_tail(self) -> None:
        encoded_tail = self._tail_text.encode("utf-8")
        max_rolling_bytes = _DEFAULT_MAX_BYTES * 2
        if len(encoded_tail) <= max_rolling_bytes:
            self._tail_bytes = len(encoded_tail)
            return

        start = len(encoded_tail) - max_rolling_bytes
        while start < len(encoded_tail) and encoded_tail[start] & 0xC0 == 0x80:
            start += 1
        self._tail_starts_at_line_boundary = (
            self._tail_starts_at_line_boundary
            if start == 0
            else encoded_tail[start - 1] == 0x0A
        )
        self._tail_text = encoded_tail[start:].decode("utf-8", errors="replace")
        self._tail_bytes = len(self._tail_text.encode("utf-8"))

    def _snapshot(self) -> _TailSnapshot:
        display_tail = self._tail_text
        if not self._tail_starts_at_line_boundary:
            first_newline = display_tail.find("\n")
            display_tail = (
                display_tail
                if first_newline == -1
                else display_tail[first_newline + 1 :]
            )

        content, truncated_by, output_lines, output_bytes, last_line_partial = (
            _truncate_tail(display_tail)
        )
        truncated = (
            self._total_lines > _DEFAULT_MAX_LINES
            or self._total_decoded_bytes > _DEFAULT_MAX_BYTES
        )
        if truncated and truncated_by is None:
            truncated_by = (
                "bytes" if self._total_decoded_bytes > _DEFAULT_MAX_BYTES else "lines"
            )
        return _TailSnapshot(
            content=content,
            truncated=truncated,
            truncated_by=truncated_by if truncated else None,
            total_lines=self._total_lines,
            total_bytes=self._total_decoded_bytes,
            output_lines=output_lines,
            output_bytes=output_bytes,
            last_line_partial=last_line_partial,
            last_line_bytes=self._current_line_bytes,
            full_output_path=None,
        )


def resolve_pi_compat_shell() -> PiCompatShellConfig:
    """Resolve Pi's Bash-first shell policy using the host-native fallback."""

    if os.name == "nt":
        comspec = os.environ.get("COMSPEC")
        candidate = comspec if comspec and comspec.strip() else "cmd.exe"
        return PiCompatShellConfig(
            executable=_resolve_shell_executable(candidate),
            arguments=("/d", "/s", "/c"),
        )

    bash_path = Path("/bin/bash")
    if bash_path.is_file():
        return PiCompatShellConfig(executable=str(bash_path), arguments=("-c",))

    bash_from_path = shutil.which("bash")
    if bash_from_path is not None:
        return PiCompatShellConfig(
            executable=_resolve_shell_executable(bash_from_path), arguments=("-c",)
        )

    return PiCompatShellConfig(
        executable=_resolve_shell_executable("sh"), arguments=("-c",)
    )


async def execute_bash(
    *,
    cwd: Path,
    command: str,
    timeout_seconds: float,
    cancellation: PiCompatCancellation,
    shell_config: PiCompatShellConfig,
) -> PiCompatOperationResult:
    """Execute one shell command with Pi-compatible capture and result mapping."""

    loop = asyncio.get_running_loop()
    operation_started = loop.time()
    validation_error = _validate_arguments(
        cwd=cwd, command=command, timeout_seconds=timeout_seconds
    )
    if validation_error is not None:
        return _result(
            text=validation_error,
            error_kind=PiCompatErrorKind.INVALID_ARGUMENTS,
            side_effect_state=PiCompatSideEffectState.NOT_ATTEMPTED,
        )

    deadline = operation_started + float(timeout_seconds)
    if cancellation.is_cancelled():
        return _result(
            text="Command aborted",
            error_kind=PiCompatErrorKind.CANCELLED,
            side_effect_state=PiCompatSideEffectState.NOT_ATTEMPTED,
        )

    accumulator = _OutputAccumulator()
    cancellation_task = asyncio.create_task(
        cancellation.wait(), name="millforge-pi-bash-cancellation-wait"
    )
    timeout_task = asyncio.create_task(
        _sleep_until(deadline), name="millforge-pi-bash-timeout"
    )
    launch_task = asyncio.create_task(
        _launch_process(
            cwd=cwd,
            command=command,
            shell_config=shell_config,
        ),
        name="millforge-pi-bash-launch",
    )
    process: asyncio.subprocess.Process | None = None
    reader_task: asyncio.Task[None] | None = None
    process_wait_task: asyncio.Task[int] | None = None
    exit_task: asyncio.Task[int] | None = None
    output_activity = asyncio.Event()
    cleanup_task: asyncio.Task[_CleanupOutcome] | None = None
    cleanup_diagnostics: list[str] = []

    def start_interruption_cleanup() -> asyncio.Task[_CleanupOutcome]:
        """Transfer launch/process ownership to one shielded cleanup task."""

        nonlocal cleanup_task
        if cleanup_task is not None:
            return cleanup_task
        if (
            process is not None
            and reader_task is not None
            and process_wait_task is not None
            and exit_task is not None
        ):
            cleanup = _finish_interrupted_process(
                process=process,
                exit_task=exit_task,
                process_wait_task=process_wait_task,
                reader_task=reader_task,
                output_activity=output_activity,
            )
        else:
            cleanup = _recover_interrupted_launch(
                launch_task=launch_task,
                accumulator=accumulator,
                output_activity=output_activity,
            )
        cleanup_task = asyncio.create_task(
            cleanup, name="millforge-pi-bash-interruption-cleanup"
        )
        return cleanup_task

    async def finish_interruption_cleanup() -> _CleanupOutcome:
        outcome = await _await_owned_task(start_interruption_cleanup())
        cleanup_diagnostics.extend(outcome.diagnostics)
        return outcome

    try:
        done, _ = await asyncio.wait(
            {launch_task, cancellation_task, timeout_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in done or cancellation.is_cancelled():
            await finish_interruption_cleanup()
            return await _interruption_result(
                accumulator=accumulator,
                status="Command aborted",
                error_kind=PiCompatErrorKind.CANCELLED,
            )
        if timeout_task in done:
            await finish_interruption_cleanup()
            return await _interruption_result(
                accumulator=accumulator,
                status=(
                    "Command timed out after "
                    f"{_format_timeout_seconds(timeout_seconds)} seconds"
                ),
                error_kind=PiCompatErrorKind.PROCESS_TIMEOUT,
            )

        try:
            process = await _observe_task(launch_task)
        except OSError as exc:
            return _result(
                text=str(exc),
                error_kind=PiCompatErrorKind.PROCESS_LAUNCH_ERROR,
                side_effect_state=PiCompatSideEffectState.NOT_ATTEMPTED,
            )
        except _LaunchInterrupted as exc:
            return _result(
                text=str(exc),
                error_kind=PiCompatErrorKind.PROCESS_LAUNCH_ERROR,
                side_effect_state=PiCompatSideEffectState.NOT_ATTEMPTED,
            )

        reader_task, process_wait_task, exit_task = _start_process_tasks(
            process, accumulator, output_activity
        )
        reader_observed = False
        while True:
            wait_set = {
                cancellation_task,
                timeout_task,
                exit_task,
                accumulator.writer_task,
            }
            if not reader_observed:
                wait_set.add(reader_task)
            # Typeshed requires one task result type although asyncio.wait
            # supports heterogeneous futures and does not inspect results here.
            done, _ = await asyncio.wait(
                wait_set,  # type: ignore[arg-type]
                return_when=asyncio.FIRST_COMPLETED,
            )

            if cancellation_task in done or cancellation.is_cancelled():
                await finish_interruption_cleanup()
                return await _interruption_result(
                    accumulator=accumulator,
                    status="Command aborted",
                    error_kind=PiCompatErrorKind.CANCELLED,
                )
            if timeout_task in done:
                await finish_interruption_cleanup()
                return await _interruption_result(
                    accumulator=accumulator,
                    status=(
                        "Command timed out after "
                        f"{_format_timeout_seconds(timeout_seconds)} seconds"
                    ),
                    error_kind=PiCompatErrorKind.PROCESS_TIMEOUT,
                )
            if reader_task in done:
                await _observe_task(reader_task)
                reader_observed = True
                continue
            if accumulator.writer_task in done:
                await _observe_task(accumulator.writer_task)
                raise RuntimeError("raw-output writer completed before process exit")

            exit_code = await _observe_task(exit_task)
            await _finish_post_exit_output(
                process=process,
                process_wait_task=process_wait_task,
                reader_task=reader_task,
                output_activity=output_activity,
            )
            snapshot = await accumulator.finish()
            text = _format_output(snapshot, empty_text="(no output)")
            if exit_code != 0:
                return _result(
                    text=_append_status(text, f"Command exited with code {exit_code}"),
                    truncated=snapshot.truncated,
                    error_kind=PiCompatErrorKind.PROCESS_EXIT_NONZERO,
                    exit_code=exit_code,
                    side_effect_state=PiCompatSideEffectState.CONFIRMED_COMPLETE,
                )
            return _result(
                text=text,
                truncated=snapshot.truncated,
                exit_code=exit_code,
                side_effect_state=PiCompatSideEffectState.CONFIRMED_COMPLETE,
            )
    except asyncio.CancelledError:
        await finish_interruption_cleanup()
        raise
    except Exception as exc:
        confirmed_complete = process is not None and (
            process.returncode is not None or _exit_is_established(exit_task)
        )
        if (
            process is not None
            and reader_task is not None
            and process_wait_task is not None
            and exit_task is not None
        ):
            if confirmed_complete:
                cleanup_diagnostics.extend(
                    await _finish_after_exit_failure(
                        process=process,
                        process_wait_task=process_wait_task,
                        reader_task=reader_task,
                    )
                )
            else:
                await finish_interruption_cleanup()
        return _io_error_result(
            accumulator=accumulator,
            error=exc,
            confirmed_complete=confirmed_complete,
        )
    finally:
        if cleanup_task is not None and not cleanup_task.done():
            try:
                outcome = await _await_owned_task(cleanup_task)
                cleanup_diagnostics.extend(outcome.diagnostics)
            except Exception as exc:
                cleanup_diagnostics.append(_format_cleanup_diagnostic(exc))
        try:
            await _cancel_and_observe(
                task
                for task in (
                    cancellation_task,
                    timeout_task,
                    launch_task if cleanup_task is None else None,
                    exit_task,
                    process_wait_task,
                    reader_task,
                )
                if task is not None and not task.done()
            )
        except Exception as exc:
            cleanup_diagnostics.append(_format_cleanup_diagnostic(exc))
        try:
            cleanup_diagnostics.extend(await accumulator.close())
        except Exception as exc:
            cleanup_diagnostics.append(_format_cleanup_diagnostic(exc))


def _resolve_shell_executable(candidate: str) -> str:
    candidate_path = Path(candidate)
    if candidate_path.is_absolute():
        if candidate_path.is_file():
            return str(candidate_path.resolve())
        raise PiCompatShellResolutionError(
            f"shell executable is not a file: {candidate}"
        )

    resolved = shutil.which(candidate)
    if resolved is None:
        raise PiCompatShellResolutionError(
            f"shell executable was not found: {candidate}"
        )
    return str(Path(resolved).resolve())


def _validate_arguments(
    *, cwd: Path, command: str, timeout_seconds: float
) -> str | None:
    try:
        cwd_text = os.fspath(cwd)
    except TypeError:
        return "cwd must be a filesystem path"
    if "\x00" in cwd_text:
        return "cwd must not contain a NUL character"
    try:
        cwd_text.encode(sys.getfilesystemencoding(), "strict")
    except UnicodeEncodeError:
        return "cwd must be filesystem-encodable"
    if not _is_absolute_cwd(cwd):
        return "cwd must be an absolute path"
    if not isinstance(command, str):
        return "command must be a string"
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        return "timeout_seconds must be a strictly positive finite number"
    return None


async def _launch_process(
    *,
    cwd: Path,
    command: str,
    shell_config: PiCompatShellConfig,
) -> asyncio.subprocess.Process:
    """Materialize a process and hand it to the caller without cleanup work."""

    return await _create_process(cwd=cwd, command=command, shell_config=shell_config)


async def _create_process(
    *, cwd: Path, command: str, shell_config: PiCompatShellConfig
) -> asyncio.subprocess.Process:
    if os.name == "nt":
        return await asyncio.create_subprocess_exec(
            shell_config.executable,
            *shell_config.arguments,
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            # One kernel pipe gives a single ordered delivery boundary for
            # bursty stdout/stderr instead of racing independent stream readers.
            stderr=asyncio.subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    return await asyncio.create_subprocess_exec(
        shell_config.executable,
        *shell_config.arguments,
        command,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )


def _start_process_tasks(
    process: asyncio.subprocess.Process,
    accumulator: _OutputAccumulator,
    output_activity: asyncio.Event,
) -> tuple[asyncio.Task[None], asyncio.Task[int], asyncio.Task[int]]:
    assert process.stdout is not None
    reader_task = asyncio.create_task(
        _read_output(process.stdout, accumulator, output_activity),
        name="millforge-pi-bash-output-reader",
    )
    process_wait_task = asyncio.create_task(
        process.wait(), name="millforge-pi-bash-process-wait"
    )
    exit_task = asyncio.create_task(
        _wait_for_process_exit(process), name="millforge-pi-bash-exit-watch"
    )
    return reader_task, process_wait_task, exit_task


async def _settle_interrupted_launch(
    launch_task: asyncio.Task[asyncio.subprocess.Process],
    cleanup_deadline: float,
) -> tuple[asyncio.subprocess.Process | None, tuple[str, ...]]:
    """Bound and observe launch handoff before an interruption result is emitted."""

    diagnostics: list[str] = []
    if not launch_task.done():
        await _wait_for_task(launch_task, _cleanup_time_remaining(cleanup_deadline))
    if not launch_task.done():
        launch_task.cancel()
        try:
            await _await_owned_task(launch_task)
        except asyncio.CancelledError:
            diagnostics.append("launch did not hand off a process before cleanup grace")
            return None, tuple(diagnostics)
        except Exception as exc:
            diagnostics.append(_format_cleanup_diagnostic(exc))
            return None, tuple(diagnostics)
    try:
        return await _observe_task(launch_task), tuple(diagnostics)
    except _LaunchInterrupted as exc:
        diagnostics.append(_format_cleanup_diagnostic(exc))
    except asyncio.CancelledError:
        diagnostics.append("launch was cancelled before process handoff")
    except Exception as exc:
        diagnostics.append(_format_cleanup_diagnostic(exc))
    return None, tuple(diagnostics)


async def _recover_interrupted_launch(
    *,
    launch_task: asyncio.Task[asyncio.subprocess.Process],
    accumulator: _OutputAccumulator,
    output_activity: asyncio.Event,
) -> _CleanupOutcome:
    """Own a racing launch until its process is reaped or handoff is bounded."""

    cleanup_deadline = _cleanup_deadline()
    process, diagnostics = await _settle_interrupted_launch(
        launch_task, cleanup_deadline
    )
    if process is None:
        return _CleanupOutcome(
            tree_confirmed=False,
            process_exited=False,
            process_reaped=False,
            diagnostics=diagnostics,
        )
    reader_task, process_wait_task, exit_task = _start_process_tasks(
        process, accumulator, output_activity
    )
    outcome = await _finish_interrupted_process(
        process=process,
        exit_task=exit_task,
        process_wait_task=process_wait_task,
        reader_task=reader_task,
        output_activity=output_activity,
        cleanup_deadline=cleanup_deadline,
    )
    return _CleanupOutcome(
        tree_confirmed=outcome.tree_confirmed,
        process_exited=outcome.process_exited,
        process_reaped=outcome.process_reaped,
        diagnostics=diagnostics + outcome.diagnostics,
    )


def _cleanup_deadline() -> float:
    return asyncio.get_running_loop().time() + _CLEANUP_GRACE_SECONDS


def _cleanup_time_remaining(deadline: float) -> float:
    return max(0.0, deadline - asyncio.get_running_loop().time())


async def _sleep_until(deadline: float) -> None:
    await asyncio.sleep(max(0.0, deadline - asyncio.get_running_loop().time()))


def _result(
    *,
    text: str,
    truncated: bool = False,
    error_kind: PiCompatErrorKind | None = None,
    exit_code: int | None = None,
    side_effect_state: PiCompatSideEffectState,
) -> PiCompatOperationResult:
    return PiCompatOperationResult(
        model_text=text,
        truncated=truncated,
        error_kind=error_kind,
        exit_code=exit_code,
        changed_path=None,
        side_effect_state=side_effect_state,
    )


async def _read_output(
    stream: asyncio.StreamReader,
    accumulator: _OutputAccumulator,
    output_activity: asyncio.Event,
) -> None:
    while chunk := await stream.read(4_096):
        await accumulator.append(chunk)
        output_activity.set()


async def _write_raw_output(queue: asyncio.Queue[bytes | None]) -> Path:
    """Write raw output off-loop and remove a partial destination on failure."""

    output_path: Path | None = None
    output_file: BinaryIO | None = None
    try:
        output_path = await _run_blocking(_create_raw_output_path)
        output_file = await _run_blocking(lambda: output_path.open("wb"))
        while True:
            data = await queue.get()
            if data is None:
                break
            assert output_file is not None
            chunk_output_file: BinaryIO = output_file
            await _run_blocking(lambda: _write_raw_chunk(chunk_output_file, data))
        assert output_file is not None
        await _run_blocking(output_file.flush)
        await _run_blocking(output_file.close)
        output_file = None
        return output_path
    except BaseException:
        if output_file is not None:
            try:
                await _run_blocking(output_file.close)
            except OSError:
                pass
        if output_path is not None:
            try:
                await _run_blocking(lambda: output_path.unlink(missing_ok=True))
            except OSError:
                pass
        raise


def _create_raw_output_path() -> Path:
    descriptor, path = tempfile.mkstemp(prefix="pi-bash-", suffix=".log")
    os.close(descriptor)
    return Path(path)


def _write_raw_chunk(output_file: BinaryIO, data: bytes) -> None:
    output_file.write(data)


async def _run_blocking(operation: Callable[[], _T]) -> _T:
    """Await one thread-bound file operation before its handle may be closed."""

    operation_task = asyncio.create_task(
        asyncio.to_thread(operation), name="millforge-pi-bash-output-thread"
    )
    cancellation: asyncio.CancelledError | None = None
    while not operation_task.done():
        try:
            await asyncio.shield(operation_task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    try:
        result = operation_task.result()
    except BaseException:
        if cancellation is not None:
            raise cancellation
        raise
    if cancellation is not None:
        raise cancellation
    return result


async def _wait_for_process_exit(process: asyncio.subprocess.Process) -> int:
    while process.returncode is None:
        await asyncio.sleep(_PROCESS_POLL_SECONDS)
    return process.returncode


async def _wait_for_process_exit_with_grace(
    process: asyncio.subprocess.Process,
    exit_task: asyncio.Task[int],
    cleanup_deadline: float,
) -> bool:
    if process.returncode is not None:
        return True
    await _wait_for_task(exit_task, _cleanup_time_remaining(cleanup_deadline))
    return process.returncode is not None


async def _finish_interrupted_process(
    *,
    process: asyncio.subprocess.Process,
    exit_task: asyncio.Task[int],
    process_wait_task: asyncio.Task[int],
    reader_task: asyncio.Task[None],
    output_activity: asyncio.Event,
    cleanup_deadline: float | None = None,
) -> _CleanupOutcome:
    cleanup_deadline = cleanup_deadline or _cleanup_deadline()
    outcome = await _terminate_process_tree(
        process, exit_task, process_wait_task, cleanup_deadline
    )
    diagnostics = list(outcome.diagnostics)
    if outcome.process_reaped:
        try:
            await _finish_post_exit_output(
                process=process,
                process_wait_task=process_wait_task,
                reader_task=reader_task,
                output_activity=output_activity,
                cleanup_deadline=cleanup_deadline,
            )
        except Exception as exc:
            diagnostics.append(_format_cleanup_diagnostic(exc))
    else:
        _close_subprocess_pipes(process)
        try:
            await _cancel_and_observe((reader_task, process_wait_task, exit_task))
        except Exception as exc:
            diagnostics.append(_format_cleanup_diagnostic(exc))
    return _CleanupOutcome(
        tree_confirmed=outcome.tree_confirmed,
        process_exited=outcome.process_exited,
        process_reaped=outcome.process_reaped,
        diagnostics=tuple(diagnostics),
    )


async def _terminate_process_tree(
    process: asyncio.subprocess.Process,
    exit_task: asyncio.Task[int],
    process_wait_task: asyncio.Task[int],
    cleanup_deadline: float,
) -> _CleanupOutcome:
    """Request tree termination, then contain the direct shell on failure."""

    diagnostics: list[str] = []
    pid = process.pid
    if pid is None:
        return _CleanupOutcome(
            tree_confirmed=False,
            process_exited=process.returncode is not None,
            process_reaped=False,
            diagnostics=("process did not expose a PID for tree cleanup",),
        )

    tree_operation = (
        _run_taskkill(pid) if os.name == "nt" else _kill_posix_process_group(pid)
    )
    tree_task = asyncio.create_task(
        tree_operation, name="millforge-pi-bash-tree-cleanup"
    )
    try:
        if await _wait_for_task(tree_task, _cleanup_time_remaining(cleanup_deadline)):
            tree_confirmed = await _observe_task(tree_task)
        else:
            tree_confirmed = False
            diagnostics.append("tree cleanup did not finish before cleanup grace")
            await _cancel_and_observe((tree_task,))
    except OSError as exc:
        tree_confirmed = False
        diagnostics.append(_format_cleanup_diagnostic(exc))
    finally:
        if not tree_task.done():
            await _cancel_and_observe((tree_task,))

    process_exited = await _wait_for_process_exit_with_grace(
        process, exit_task, cleanup_deadline
    )
    if process_exited:
        _close_subprocess_pipes(process)
        process_reaped, wait_diagnostics = await _settle_process_wait(
            process, process_wait_task, cleanup_deadline
        )
        diagnostics.extend(wait_diagnostics)
        if tree_confirmed and process_reaped:
            return _CleanupOutcome(
                tree_confirmed=True,
                process_exited=True,
                process_reaped=True,
                diagnostics=tuple(diagnostics),
            )

    direct_exited, direct_reaped, direct_diagnostics = await _terminate_direct_child(
        process, exit_task, process_wait_task, cleanup_deadline
    )
    diagnostics.extend(direct_diagnostics)
    return _CleanupOutcome(
        tree_confirmed=tree_confirmed,
        process_exited=direct_exited,
        process_reaped=direct_reaped,
        diagnostics=tuple(diagnostics),
    )


async def _kill_posix_process_group(pid: int) -> bool:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return not _posix_process_group_exists(pid)
    except OSError:
        return False
    return await _wait_for_posix_process_group_death(pid)


def _posix_process_group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


async def _wait_for_posix_process_group_death(pid: int) -> bool:
    deadline = asyncio.get_running_loop().time() + _CLEANUP_GRACE_SECONDS
    while _posix_process_group_exists(pid):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(_PROCESS_POLL_SECONDS)
    return True


async def _run_taskkill(pid: int) -> bool:
    try:
        taskkill = await asyncio.create_subprocess_exec(
            "taskkill",
            "/F",
            "/T",
            "/PID",
            str(pid),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False

    wait_task = asyncio.create_task(
        taskkill.wait(), name="millforge-pi-bash-taskkill-wait"
    )
    try:
        if not await _wait_for_task(wait_task, _CLEANUP_GRACE_SECONDS):
            try:
                taskkill.kill()
            except (OSError, ProcessLookupError):
                pass
            if await _wait_for_task(wait_task, _CLEANUP_GRACE_SECONDS):
                await _observe_task(wait_task)
            else:
                await _cancel_and_observe((wait_task,))
            return False
        return await _observe_task(wait_task) == 0
    finally:
        await _cancel_and_observe(task for task in (wait_task,) if not task.done())


async def _terminate_direct_child(
    process: asyncio.subprocess.Process,
    exit_task: asyncio.Task[int],
    process_wait_task: asyncio.Task[int],
    cleanup_deadline: float,
) -> tuple[bool, bool, tuple[str, ...]]:
    diagnostics: list[str] = []
    if process.returncode is None:
        try:
            process.kill()
        except (OSError, ProcessLookupError) as exc:
            diagnostics.append(_format_cleanup_diagnostic(exc))
    process_exited = await _wait_for_process_exit_with_grace(
        process, exit_task, cleanup_deadline
    )
    _close_subprocess_pipes(process)
    process_reaped, wait_diagnostics = await _settle_process_wait(
        process, process_wait_task, cleanup_deadline
    )
    diagnostics.extend(wait_diagnostics)
    return process_exited, process_reaped, tuple(diagnostics)


async def _finish_post_exit_output(
    *,
    process: asyncio.subprocess.Process,
    process_wait_task: asyncio.Task[int],
    reader_task: asyncio.Task[None],
    output_activity: asyncio.Event,
    cleanup_deadline: float | None = None,
) -> None:
    reader_error: Exception | None = None
    try:
        reader_finished = await _drain_post_exit_output(reader_task, output_activity)
        if not reader_finished:
            _close_subprocess_pipes(process)
            await _cancel_and_observe((reader_task,))
        else:
            await _observe_task(reader_task)
    except Exception as exc:
        reader_error = exc
    finally:
        await _settle_process_wait(
            process,
            process_wait_task,
            cleanup_deadline or _cleanup_deadline(),
        )
    if reader_error is not None:
        raise reader_error


async def _finish_after_exit_failure(
    *,
    process: asyncio.subprocess.Process,
    process_wait_task: asyncio.Task[int],
    reader_task: asyncio.Task[None],
) -> tuple[str, ...]:
    diagnostics: list[str] = []
    cleanup_deadline = _cleanup_deadline()
    _close_subprocess_pipes(process)
    try:
        await _cancel_and_observe((reader_task,))
    except Exception as exc:
        diagnostics.append(_format_cleanup_diagnostic(exc))
    _, wait_diagnostics = await _settle_process_wait(
        process, process_wait_task, cleanup_deadline
    )
    diagnostics.extend(wait_diagnostics)
    return tuple(diagnostics)


async def _settle_process_wait(
    process: asyncio.subprocess.Process,
    process_wait_task: asyncio.Task[int],
    cleanup_deadline: float,
) -> tuple[bool, tuple[str, ...]]:
    diagnostics: list[str] = []
    if await _wait_for_task(
        process_wait_task, _cleanup_time_remaining(cleanup_deadline)
    ):
        try:
            await _observe_task(process_wait_task)
        except Exception as exc:
            diagnostics.append(_format_cleanup_diagnostic(exc))
            return False, tuple(diagnostics)
        return True, tuple(diagnostics)
    _close_subprocess_pipes(process)
    if await _wait_for_task(
        process_wait_task, _cleanup_time_remaining(cleanup_deadline)
    ):
        try:
            await _observe_task(process_wait_task)
        except Exception as exc:
            diagnostics.append(_format_cleanup_diagnostic(exc))
            return False, tuple(diagnostics)
        return True, tuple(diagnostics)
    try:
        await _cancel_and_observe((process_wait_task,))
    except Exception as exc:
        diagnostics.append(_format_cleanup_diagnostic(exc))
    return False, tuple(diagnostics)


async def _drain_post_exit_output(
    reader_task: asyncio.Task[None], output_activity: asyncio.Event
) -> bool:
    """Drain output until EOF or Pi's re-armed 100-ms idle window expires."""

    loop = asyncio.get_running_loop()
    idle_deadline = loop.time() + _POST_EXIT_IDLE_SECONDS
    output_activity.clear()
    while not reader_task.done():
        remaining = idle_deadline - loop.time()
        if remaining <= 0:
            return False
        activity_task = asyncio.create_task(
            output_activity.wait(), name="millforge-pi-bash-post-exit-activity"
        )
        try:
            done, _ = await asyncio.wait(
                {reader_task, activity_task},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if reader_task.done():
                return True
            if activity_task in done:
                await _observe_task(activity_task)
                output_activity.clear()
                idle_deadline = loop.time() + _POST_EXIT_IDLE_SECONDS
        finally:
            if not activity_task.done():
                await _cancel_and_observe((activity_task,))
    return True


def _close_subprocess_pipes(process: asyncio.subprocess.Process) -> None:
    """Close inherited pipe transports once Pi's idle window has elapsed."""

    transport = getattr(process, "_transport", None)
    if transport is None:
        return
    pipe_transport = transport.get_pipe_transport(1)
    if pipe_transport is not None:
        pipe_transport.close()


async def _wait_for_task(task: asyncio.Task[object], timeout: float) -> bool:
    done, _ = await asyncio.wait({task}, timeout=timeout)
    return task in done


async def _observe_task(task: asyncio.Task[_T]) -> _T:
    return await task


async def _await_owned_task(task: asyncio.Task[_T]) -> _T:
    """Observe a task even if its parent receives cancellation mid-cleanup."""

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    try:
        result = task.result()
    except BaseException:
        if cancellation is not None:
            raise cancellation
        raise
    if cancellation is not None:
        raise cancellation
    return result


async def _cancel_and_observe(tasks: Iterable[asyncio.Task[object]]) -> None:
    pending = tuple(tasks)
    for task in pending:
        if not task.done():
            task.cancel()
    error: Exception | None = None
    for task in pending:
        if not task.done() and not await _wait_for_task(task, _CLEANUP_GRACE_SECONDS):
            continue
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if error is None:
                error = exc
    if error is not None:
        raise error


async def _interruption_result(
    *,
    accumulator: _OutputAccumulator,
    status: str,
    error_kind: PiCompatErrorKind,
) -> PiCompatOperationResult:
    try:
        snapshot = await accumulator.finish()
    except Exception:
        snapshot = accumulator.snapshot_after_failure()
    return _result(
        text=_append_status(_format_output(snapshot, empty_text=""), status),
        truncated=snapshot.truncated,
        error_kind=error_kind,
        side_effect_state=PiCompatSideEffectState.COMPLETION_UNKNOWN,
    )


def _io_error_result(
    *,
    accumulator: _OutputAccumulator,
    error: Exception,
    confirmed_complete: bool,
) -> PiCompatOperationResult:
    snapshot = accumulator.snapshot_after_failure()
    return _result(
        text=_append_status(_format_output(snapshot, empty_text=""), str(error)),
        truncated=snapshot.truncated,
        error_kind=PiCompatErrorKind.IO_ERROR,
        side_effect_state=(
            PiCompatSideEffectState.CONFIRMED_COMPLETE
            if confirmed_complete
            else PiCompatSideEffectState.COMPLETION_UNKNOWN
        ),
    )


def _exit_is_established(exit_task: asyncio.Task[int] | None) -> bool:
    if exit_task is None or not exit_task.done() or exit_task.cancelled():
        return False
    return exit_task.exception() is None


def _format_cleanup_diagnostic(error: Exception) -> str:
    message = str(error).replace("\x00", "\\0")
    return f"{type(error).__name__}: {message}"


def _format_timeout_seconds(timeout_seconds: float) -> str:
    """Render a finite IEEE-754 value using JavaScript Number#toString rules."""

    timeout = float(timeout_seconds)
    if timeout == 0:
        return "0"

    text = repr(timeout).lower()
    magnitude = abs(timeout)
    if 1e-6 <= magnitude < 1e21:
        return _scientific_to_decimal(text) if "e" in text else text.removesuffix(".0")

    if "e" not in text:
        return text.removesuffix(".0")
    mantissa, exponent_text = text.split("e", maxsplit=1)
    mantissa = mantissa.removesuffix(".0")
    exponent = int(exponent_text)
    sign = "+" if exponent >= 0 else "-"
    return f"{mantissa}e{sign}{abs(exponent)}"


def _scientific_to_decimal(text: str) -> str:
    mantissa, exponent_text = text.split("e", maxsplit=1)
    exponent = int(exponent_text)
    sign = ""
    if mantissa.startswith("-"):
        sign, mantissa = "-", mantissa[1:]
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    decimal_index = len(whole) + exponent
    if decimal_index <= 0:
        return f"{sign}0.{('0' * -decimal_index)}{digits}"
    if decimal_index >= len(digits):
        return f"{sign}{digits}{('0' * (decimal_index - len(digits)))}"
    return f"{sign}{digits[:decimal_index]}.{digits[decimal_index:]}"


def _format_output(snapshot: _TailSnapshot, *, empty_text: str) -> str:
    text = snapshot.content or empty_text
    if not snapshot.truncated or snapshot.full_output_path is None:
        return text

    start_line = snapshot.total_lines - snapshot.output_lines + 1
    end_line = snapshot.total_lines
    if snapshot.last_line_partial:
        last_line_size = _format_size(snapshot.last_line_bytes)
        return _append_status(
            text,
            f"[Showing last {_format_size(snapshot.output_bytes)} of line {end_line} "
            f"(line is {last_line_size}). Full output: {snapshot.full_output_path}]",
        )
    if snapshot.truncated_by == "lines":
        notice = f"[Showing lines {start_line}-{end_line} of {snapshot.total_lines}. Full output: {snapshot.full_output_path}]"
    else:
        notice = (
            f"[Showing lines {start_line}-{end_line} of {snapshot.total_lines} "
            f"({_format_size(_DEFAULT_MAX_BYTES)} limit). Full output: {snapshot.full_output_path}]"
        )
    return _append_status(text, notice)


def _append_status(text: str, status: str) -> str:
    return f"{text}\n\n{status}" if text else status


def _truncate_tail(content: str) -> tuple[str, str | None, int, int, bool]:
    truncation = truncate_tail(
        content,
        max_lines=_DEFAULT_MAX_LINES,
        max_bytes=_DEFAULT_MAX_BYTES,
    )
    return (
        truncation.content,
        truncation.truncated_by,
        truncation.output_lines,
        truncation.output_bytes,
        truncation.last_line_partial,
    )


def _format_size(byte_count: int) -> str:
    return format_size(byte_count)
