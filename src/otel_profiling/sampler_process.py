import base64
import ctypes
import json
import logging
import os
import select
import subprocess
import sys
from typing import TYPE_CHECKING

from otel_profiling.span_table import SpanTable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opentelemetry.proto.resource.v1.resource_pb2 import Resource

    from otel_profiling.profiler import ProfileType
    from otel_profiling.span_tracker import SpanTracker

logger = logging.getLogger(__name__)

_PR_SET_PTRACER = 0x59616D61
_READY_TIMEOUT = 10
_STOP_TIMEOUT = 15
_CHILD_ENTRYPOINT = "import sys; sys.path[:0] = {path!r}; from otel_profiling._process import main; main()"


def supports_process_mode() -> bool:
    """Whether this platform has what the sampler process needs (``memfd_create``, ``prctl``, ``/proc``)."""
    return sys.platform == "linux" and hasattr(os, "memfd_create")


class SamplerProcessError(RuntimeError):
    """The sampler process could not be started or cannot read this process."""


class SamplerProcess:
    """Child process that samples this process without competing for its GIL.

    The child is a fresh interpreter (not a fork of the application) started in isolated mode,
    so it doesn't run the application's ``sitecustomize`` or auto-instrumentation. This process
    allows it to read its memory with ``PR_SET_PTRACER``, so no ``CAP_SYS_PTRACE`` is needed.
    Span links are shared through a ``SpanTable`` in shared memory, and the child exits when
    its stdin is closed, including when this process dies.
    """

    def __init__(
        self,
        resource: Resource,
        endpoint: str,
        headers: Mapping[str, str],
        interval: float,
        export_interval: float,
        profile_types: tuple[ProfileType, ...],
        span_tracker: SpanTracker | None = None,
    ) -> None:
        self.span_tracker = span_tracker
        self.table = SpanTable.create() if span_tracker else None
        self._config = {
            "pid": os.getpid(),
            "resource": base64.b64encode(resource.SerializeToString()).decode(),
            "endpoint": endpoint,
            "headers": dict(headers),
            "interval": interval,
            "export_interval": export_interval,
            "profile_types": list(profile_types),
            "table_fd": self.table.fd if self.table else None,
        }
        self._process: subprocess.Popen[str] | None = None

    @property
    def is_alive(self) -> bool:
        """Whether the sampler process is running."""
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> int | None:
        """Process id of the sampler, if started."""
        return self._process.pid if self._process else None

    def start(self) -> None:
        """Start the sampler and wait until it confirms it can read this process."""
        path = [entry for entry in sys.path if entry and "auto_instrumentation" not in entry]
        process = subprocess.Popen(  # noqa: S603
            [sys.executable, "-I", "-c", _CHILD_ENTRYPOINT.format(path=path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            pass_fds=(self.table.fd,) if self.table else (),
        )
        self._process = process
        _allow_ptrace(process.pid)
        assert process.stdin is not None  # noqa: S101
        assert process.stdout is not None  # noqa: S101
        process.stdin.write(json.dumps(self._config) + "\n")
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], _READY_TIMEOUT)
        reply = process.stdout.readline().strip() if ready else "timeout"
        if reply != "ready":
            self.stop()
            msg = f"Sampler process failed to start: {reply}"
            raise SamplerProcessError(msg)

        if self.span_tracker:
            self.span_tracker.table = self.table

    def stop(self) -> None:
        """Ask the sampler to export the remaining samples and exit."""
        if self.span_tracker and self.span_tracker.table is self.table:
            self.span_tracker.table = None

        if self._process is not None:
            if self._process.stdin:
                self._process.stdin.close()

            try:
                self._process.wait(_STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self._process.kill()

            if self._process.stdout:
                self._process.stdout.close()

            self._process = None

        if self.table:
            self.table.close()
            self.table = None

    def abandon(self) -> None:
        """Drop this handle in a forked child without stopping the sampler of the parent."""
        if self.span_tracker and self.span_tracker.table is self.table:
            self.span_tracker.table = None

        if self._process is not None:
            for stream in (self._process.stdin, self._process.stdout):
                if stream:
                    stream.close()

            self._process = None

        if self.table:
            self.table.close()
            self.table = None


def _allow_ptrace(pid: int) -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_PTRACER, pid, 0, 0, 0)
    except OSError, AttributeError:
        logger.debug("PR_SET_PTRACER is not available", exc_info=True)
