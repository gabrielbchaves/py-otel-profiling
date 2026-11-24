import mmap
import os
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from otel_profiling.span_tracker import SpanLink

_SLOT = struct.Struct("<QQQQ")
_DEFAULT_SLOTS = 1024


class SpanTable:
    """Fixed-size table in shared memory mapping native thread ids to their active span.

    The instrumented process writes one slot per thread and the sampler process reads the
    whole table on every sample. Each slot holds the thread id, the trace id (as two 64-bit
    halves) and the span id; a zero span id means no active span.
    """

    def __init__(self, fd: int, slots: int = _DEFAULT_SLOTS, *, create: bool = False) -> None:
        self.fd = fd
        self.slots = slots
        size = slots * _SLOT.size
        if create:
            os.ftruncate(fd, size)

        self._memory = mmap.mmap(fd, size)
        self._index: dict[int, int] = {}

    @classmethod
    def create(cls, slots: int = _DEFAULT_SLOTS) -> SpanTable:
        """Allocate an anonymous shared memory table that can be passed to a child process."""
        fd = os.memfd_create("otel-profiling-spans", 0)
        return cls(fd, slots, create=True)

    def set(self, thread_id: int, link: SpanLink | None) -> None:
        """Publish the active span of a thread, or clear it."""
        slot = self._index.get(thread_id)
        if slot is None:
            if link is None or len(self._index) >= self.slots:
                return

            slot = self._index[thread_id] = len(self._index)

        trace_id, span_id = link or (0, 0)
        _SLOT.pack_into(
            self._memory, slot * _SLOT.size, thread_id, trace_id >> 64, trace_id & (2**64 - 1), span_id
        )

    def read(self) -> dict[int, SpanLink]:
        """Return the active span of every thread that has one."""
        links: dict[int, SpanLink] = {}
        for thread_id, trace_high, trace_low, span_id in _SLOT.iter_unpack(self._memory):
            if thread_id and span_id:
                links[thread_id] = ((trace_high << 64) | trace_low, span_id)

        return links

    def clear(self) -> None:
        """Forget every slot."""
        self._memory[:] = bytes(len(self._memory))
        self._index.clear()

    def close(self) -> None:
        """Release the mapping and its file descriptor."""
        self._memory.close()
        os.close(self.fd)
