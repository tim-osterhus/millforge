"""Live-only cooperative control for the production-owned local IO profile.

The reservation and every effect entry share one lock. This is not a checkpoint,
provider cancellation API, filesystem sandbox or process-tree supervisor.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys
from datetime import datetime, UTC
import hashlib
import json
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, ClassVar, Literal
from types import TracebackType

PROFILE = {
    "profile_id": "millforge-base.cooperative-local-io.v1",
    "selected_adapter": "millforge",
    "native_state": "running",
    "effect_boundary": "stock-local-read-write-and-owned-http-v1",
}
PROFILE_DIGEST = hashlib.sha256(
    json.dumps(PROFILE, sort_keys=True).encode()
).hexdigest()
CURRENT_CONTROL: ContextVar[NativePauseControl | None] = ContextVar(
    "millforge_native_control", default=None
)


def local_file_supported(root: Path, path: object, *, writing: bool) -> bool:
    """Observe ordinary, contained, non-symlink local paths; no general sandbox."""
    if not isinstance(path, str) or not path or "\x00" in path:
        return False
    try:
        if not local_filesystem(root):
            return False
        target = Path(path)
        target = target if target.is_absolute() else root / target
        target = Path(target.absolute())
        target.relative_to(root)
        if ".." in target.parts:
            return False
        for ancestor in (*reversed(target.parents), target):
            if ancestor == root or root in ancestor.parents:
                try:
                    mode = ancestor.lstat().st_mode
                except FileNotFoundError:
                    if writing:
                        continue
                    return False
                if ancestor.stat().st_dev != root.stat().st_dev or stat.S_ISLNK(mode):
                    return False
                if ancestor == target:
                    if not stat.S_ISREG(mode):
                        return False
                elif not stat.S_ISDIR(mode):
                    return False
        return target.resolve().is_relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return False


class NativePauseControl:
    """One actual invocation; external labels do not establish its eligibility."""

    def __init__(self, root: Path, *, eligible: bool) -> None:
        self.root = root.resolve()
        self.owner_id = str(uuid.uuid4())
        self._lock = threading.RLock()
        self._eligible = eligible
        self._state = "constructing"
        self._reason: str | None = None if eligible else "unqualified_composition"
        self._active = 0
        self._sequence = 0
        self._pause_id: str | None = None
        self._pending: str | None = None
        self._operations: dict[str, dict[str, Any]] = {}
        self._preempt: Callable[[], None] = lambda: None
        self._journal: Callable[[dict[str, Any]], None] | None = None
        self._journal_failed = False
        self._barrier = False
        self._parked = False
        self._session_id: str | None = None
        self._invocation_id: int | None = None
        self._effects: dict[str, int] = {}

    def ready(self, session_id: str, preempt: Callable[[], None]) -> None:
        with self._lock:
            if self._state != "constructing":
                raise RuntimeError("native_control_already_bound")
            self._session_id = session_id
            self._invocation_id = id(asyncio.current_task())
            self._preempt = preempt
            self._state = "running"

    def set_journal(self, journal: Callable[[dict[str, Any]], None] | None) -> None:
        with self._lock:
            self._journal = journal

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "owner_id": self.owner_id,
                "profile": {**PROFILE, "profile_digest": PROFILE_DIGEST},
                "eligible": self._eligible,
                "invalidation_pending": self._journal_failed,
                "state": self._state,
                "reason": self._reason,
                "sequence": self._sequence,
                "active_effects": self._active,
                "parked": self._parked,
                "effect_counts": dict(self._effects),
                "os_descendants": [] if self._eligible else None,
                "descendant_boundary": "no-processes-before-unsupported-entry"
                if self._eligible
                else "unknown_after_unqualified_entry",
                "session_id": self._session_id,
                "invocation_id": self._invocation_id,
                "pause_id": self._pause_id,
                "pending": self._pending,
                "operations": {
                    key: dict(value) for key, value in self._operations.items()
                },
            }

    @contextmanager
    def reservation(
        self, profile: dict[str, Any], action: str, pause_id: str | None
    ) -> Iterator[None]:
        # Never block a daemon listener behind a native effect/admission.
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("native_control_busy")
        try:
            try:
                self._preempt()
            except BaseException:
                self.invalidate("native_preempted")
                raise RuntimeError("native_authority_preempted") from None
            if profile != {**PROFILE, "profile_digest": PROFILE_DIGEST}:
                raise RuntimeError("native_profile_mismatch")
            if not self._eligible or self._journal_failed or self._journal is None:
                raise RuntimeError("native_profile_unqualified")
            if action == "runs.pause":
                if self._state != "running" or self._pending is not None:
                    raise RuntimeError("native_state_unsupported")
            elif action == "runs.resume":
                if self._state != "held" or pause_id != self._pause_id:
                    raise RuntimeError("native_pause_mismatch")
            else:
                raise RuntimeError("native_action_unsupported")
            yield
        finally:
            self._lock.release()

    def accept(self, key: str, digest: str, action: str, pause_id: str) -> None:
        """Called under reservation, only after durable acceptance commits."""
        with self._lock:
            prior = self._operations.get(key)
            if prior is not None:
                if prior["digest"] != digest:
                    raise RuntimeError("native_operation_conflict")
                return
            if len(self._operations) >= 2:
                self._operations.pop(next(iter(self._operations)))
            self._operations[key] = {"digest": digest, "stage": "accepted_pending"}
            self._pending = key
            self._pause_id = pause_id
            if action == "runs.resume":
                self._state = "running"
                # Hold is released, but an effect persistence barrier remains
                # until Core has recorded the release. Death cannot reuse it.
                self._barrier = True
                self._settle("applied")
            else:
                self._state = "pause_pending"

    def confirm_settlement(self, key: str) -> None:
        with self._lock:
            if key in self._operations and self._operations[key]["stage"] == "applied":
                self._barrier = False

    def _settle(self, stage: str) -> None:
        if self._pending is not None:
            self._operations[self._pending].update(stage=stage, sequence=self._sequence)
            self._pending = None

    def _quiesce(self) -> None:
        if self._state == "pause_pending" and self._active == 0 and self._parked:
            try:
                self._preempt()
            except BaseException:
                self.invalidate("native_preempted")
                return
            self._state = "held"
            self._sequence += 1
            self._settle("applied")

    def invalidate(self, reason: str) -> None:
        with self._lock:
            # A paused witness must be invalidated durably before any subsequent
            # effect (including cancellation finalization) is permitted.
            if self._state == "held" or self._journal_failed:
                self._journal_failed = True
                if self._journal is None:
                    raise RuntimeError("native_invalidation_journal_missing")
                self._journal({"owner_id": self.owner_id, "reason": reason})
                self._journal_failed = False
            self._eligible = False
            if not (self._reason or "").startswith("unsupported_"):
                self._reason = reason
            self._state = "closed"
            self._barrier = False
            self._sequence += 1
            self._settle("unknown")

    async def checkpoint(self) -> None:
        while True:
            with self._lock:
                try:
                    self._preempt()
                except BaseException:
                    self.invalidate("native_preempted")
                    raise
                self._parked = self._state in {"held", "pause_pending"} or self._barrier
                self._quiesce()
                waiting = self._state in {"held", "pause_pending"} or self._barrier
            if not waiting:
                return
            await asyncio.sleep(0.01)

    def effect(self, kind: str, *, supported: bool) -> _NativeEffect:
        return _NativeEffect(self, kind, supported)

    async def enter_effect(self, kind: str, supported: bool) -> None:
        while True:
            await self.checkpoint()
            with self._lock:
                if self._state in {"held", "pause_pending"} or self._barrier:
                    continue
                if not supported:
                    self.invalidate("unsupported_" + kind)
                self._active += 1
                self._sequence += 1
                self._effects[kind] = self._effects.get(kind, 0) + 1
                break

    def leave_effect(self, kind: str, failed: bool) -> None:
        try:
            if failed:
                self.invalidate("unknown_" + kind)
        finally:
            with self._lock:
                self._active -= 1
                self._sequence += 1


class _NativeEffect:
    # A class context manager preserves frozen exception instances. Generator
    # context managers assign __traceback__ and can replace provider errors.
    def __init__(
        self, control: NativePauseControl | None, kind: str, supported: bool
    ) -> None:
        self.control, self.kind, self.supported = control, kind, supported

    async def __aenter__(self) -> None:
        if self.control is not None:
            await self.control.enter_effect(self.kind, self.supported)

    async def __aexit__(
        self,
        typ: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> Literal[False]:
        if self.control is not None:
            self.control.leave_effect(self.kind, error is not None)
        return False


def native_effect(kind: str, *, supported: bool) -> _NativeEffect:
    return _NativeEffect(CURRENT_CONTROL.get(), kind, supported)


class _StatFS(ctypes.Structure):
    # macOS SDK sys/mount.h, __DARWIN_STRUCT_STATFS64.
    _fields_: ClassVar[list[tuple[str, Any]]] = (
        [("bsize", ctypes.c_uint32), ("iosize", ctypes.c_int32)]
        + [
            (name, ctypes.c_uint64)
            for name in ("blocks", "bfree", "bavail", "files", "ffree")
        ]
        + [("fsid", ctypes.c_int32 * 2)]
        + [(name, ctypes.c_uint32) for name in ("owner", "type", "flags", "subtype")]
        + [
            ("fstype", ctypes.c_char * 16),
            ("mount", ctypes.c_char * 1024),
            ("source", ctypes.c_char * 1024),
            ("extended", ctypes.c_uint32),
            ("reserved", ctypes.c_uint32 * 7),
        ]
    )


def local_filesystem(root: Path) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        call = libc.statfs
        call.argtypes = [ctypes.c_char_p, ctypes.POINTER(_StatFS)]
        call.restype = ctypes.c_int
        result = _StatFS()
        return (
            call(os.fsencode(root), ctypes.byref(result)) == 0
            and bool(result.flags & 0x1000)
            and result.fstype in {b"apfs", b"hfs"}
        )
    except (OSError, AttributeError):
        return False


class NativeClock:
    def utc_now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class NativeCancellationToken:
    def __init__(self, cancellation_id: str) -> None:
        self.cancellation_id = cancellation_id
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def cancel(self, reason: str) -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason
            self._event.set()
            return True

    async def wait(self) -> None:
        while not self.is_cancelled():
            await asyncio.sleep(0.01)


class NativeCancellationResolver:
    def __init__(self, token: NativeCancellationToken) -> None:
        if type(token) is not NativeCancellationToken:
            raise TypeError("native_token_required")
        self.token = token

    def resolve(self, ref: Any) -> NativeCancellationToken:
        if ref.cancellation_id != self.token.cancellation_id:
            raise ValueError("native_cancellation_identity")
        return self.token


class NativeEnvironmentSecretResolver:
    def __init__(self, expected: Any) -> None:
        self.expected = expected

    def resolve(self, ref: Any) -> Any:
        from millforge.model_backend import ResolvedSecret

        if ref != self.expected or not ref.env_var:
            raise ValueError("native_secret_identity")
        return ResolvedSecret(os.environ[ref.env_var])
