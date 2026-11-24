"""UTF-8 observation collector adapted from Nano RLM's MIT-licensed output helper."""

# Copyright (c) 2026 Prime Intellect
# Distributed under the terms in LICENSE.nano-rlm.
from dataclasses import dataclass


MARKER = b"\n[... output omitted ...]\n"


@dataclass(frozen=True)
class OutputSize:
    nbytes: int = 0

    def __add__(self, other: "OutputSize") -> "OutputSize":
        return OutputSize(self.nbytes + other.nbytes)


def _char_start(data: bytes, at: int) -> int:
    """Move a cut back to the start of its UTF-8 character."""
    while 0 < at < len(data) and data[at] & 0xC0 == 0x80:
        at -= 1
    return at


def _suffix(data: bytes, keep: int) -> bytes:
    start = max(0, len(data) - keep)
    while start < len(data) and data[start] & 0xC0 == 0x80:
        start += 1
    return data[start:]


class OutputCapture:
    """Keep ordered text, including the omission marker, within one byte budget."""

    def __init__(self, limit: int):
        self.limit = limit
        self.size = OutputSize()
        self._head = b""
        self._tail = b""

    def add(self, text: str) -> None:
        data = text.encode("utf-8", "strict")
        previous = self.size.nbytes
        self.size += OutputSize(len(data))
        if self.size.nbytes <= self.limit:
            self._head += data
            return

        if len(MARKER) > self.limit:
            if previous <= self.limit:
                prefix = self._head + data[: self.limit + 4]
                self._head = prefix[: _char_start(prefix, self.limit)]
            return

        keep = self.limit - len(MARKER)
        head_limit = keep // 2
        tail_limit = keep - head_limit
        if previous <= self.limit:
            prefix = self._head + data[: head_limit + 4]
            self._tail = self._head
            self._head = prefix[: _char_start(prefix, min(head_limit, len(prefix)))]
        suffix = data if len(data) > tail_limit else self._tail + data
        self._tail = _suffix(suffix, tail_limit)

    def result(self) -> tuple[str, bool]:
        truncated = self.size.nbytes > self.limit
        if truncated and len(MARKER) <= self.limit:
            return (self._head + MARKER + self._tail).decode("utf-8"), True
        return self._head.decode("utf-8"), truncated
