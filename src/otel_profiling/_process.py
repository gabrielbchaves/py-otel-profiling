"""Sampler process that profiles its parent.

Started by ``ProfilingInstrumentor`` in ``process`` mode. It reads one JSON configuration line
from stdin, checks it can read the target process memory, answers ``ready`` on stdout, and
samples until stdin is closed, which happens when the target stops the profiler or dies.
"""

import _remote_debugging
import base64
import json
import signal
import sys
from typing import Any

from opentelemetry.proto.resource.v1.resource_pb2 import Resource

from otel_profiling.profiler import Profiler
from otel_profiling.span_table import SpanTable


def main() -> None:
    """Profile the process described by the configuration read from stdin."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    config: dict[str, Any] = json.loads(sys.stdin.readline())
    try:
        _remote_debugging.RemoteUnwinder(config["pid"]).get_stack_trace()
    except Exception as error:  # noqa: BLE001
        _reply(f"error {type(error).__name__}: {error}")
        sys.exit(1)

    table_fd = config.get("table_fd")
    profiler = Profiler(
        resource=Resource.FromString(base64.b64decode(config["resource"])),
        endpoint=config["endpoint"],
        headers=config["headers"],
        interval=config["interval"],
        export_interval=config["export_interval"],
        profile_types=tuple(config["profile_types"]),
        pid=config["pid"],
        span_table=SpanTable(table_fd) if table_fd is not None else None,
    )
    profiler.start()
    _reply("ready")
    sys.stdin.read()
    profiler.stop()


def _reply(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
