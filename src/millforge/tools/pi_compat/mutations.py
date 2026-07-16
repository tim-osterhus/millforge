"""In-process serialization for writes and edits targeting one filesystem path."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

__all__ = ["file_mutation_lock"]

_registration_lock = threading.Lock()


@dataclass
class _PathLockEntry:
    lock: threading.RLock
    users: int = 0


_path_locks: dict[str, _PathLockEntry] = {}


def _mutation_key(path: Path) -> str:
    absolute = Path(os.path.abspath(path))
    try:
        return str(absolute.resolve(strict=True))
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.normpath(str(absolute)))


@contextmanager
def file_mutation_lock(path: Path) -> Iterator[None]:
    """Serialize mutations to the same existing or lexical target path.

    ``users`` includes both lock holders and queued callers, so the entry can
    disappear only after the last caller has released it.
    """

    key = _mutation_key(path)
    with _registration_lock:
        entry = _path_locks.get(key)
        if entry is None:
            entry = _PathLockEntry(lock=threading.RLock())
            _path_locks[key] = entry
        entry.users += 1

    try:
        with entry.lock:
            yield
    finally:
        with _registration_lock:
            entry.users -= 1
            if entry.users == 0 and _path_locks.get(key) is entry:
                del _path_locks[key]
