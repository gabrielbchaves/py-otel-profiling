import _remote_debugging
import logging
import os
import threading
import time
import urllib.request
from collections import Counter
from profiling.sampling.constants import (
    PROFILING_MODE_WALL,
    THREAD_STATUS_GIL_REQUESTED,
    THREAD_STATUS_HAS_GIL,
    THREAD_STATUS_ON_CPU,
)
from typing import TYPE_CHECKING, Literal

from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.proto.collector.profiles.v1development.profiles_service_pb2 import (
    ExportProfilesServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.profiles.v1development.profiles_pb2 import (
    Function,
    Line,
    Location,
    Profile,
    ProfilesDictionary,
    ResourceProfiles,
    Sample,
    ScopeProfiles,
    Stack,
    ValueType,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opentelemetry.util.types import AttributeValue

logger = logging.getLogger(__name__)

type Frame = tuple[str, str, int]
type StackKey = tuple[Frame, ...]
type ProfileType = Literal["cpu", "wall"]
type Samples = dict[ProfileType, Counter[StackKey]]

PROFILE_TYPES: tuple[ProfileType, ...] = ("cpu", "wall")

_SCOPE_NAME = "otel_profiling"
_NANOS_PER_SECOND = 1_000_000_000
_RUNNING = THREAD_STATUS_ON_CPU | THREAD_STATUS_HAS_GIL | THREAD_STATUS_GIL_REQUESTED


class _Dictionary:
    def __init__(self) -> None:
        self.strings: dict[str, int] = {"": 0}
        self.functions: dict[tuple[str, str], int] = {("", ""): 0}
        self.locations: dict[Frame, int] = {}
        self.stacks: dict[StackKey, int] = {(): 0}
        self.function_table = [Function()]
        self.location_table = [Location()]
        self.stack_table = [Stack()]

    def string(self, value: str) -> int:
        return self.strings.setdefault(value, len(self.strings))

    def function(self, filename: str, name: str) -> int:
        key = (filename, name)
        if key not in self.functions:
            self.functions[key] = len(self.function_table)
            self.function_table.append(
                Function(name_strindex=self.string(name), filename_strindex=self.string(filename)),
            )

        return self.functions[key]

    def location(self, frame: Frame) -> int:
        if frame not in self.locations:
            filename, name, lineno = frame
            self.locations[frame] = len(self.location_table)
            self.location_table.append(
                Location(lines=[Line(function_index=self.function(filename, name), line=lineno)]),
            )

        return self.locations[frame]

    def stack(self, key: StackKey) -> int:
        if key not in self.stacks:
            self.stacks[key] = len(self.stack_table)
            self.stack_table.append(Stack(location_indices=[self.location(frame) for frame in key]))

        return self.stacks[key]

    def to_proto(self) -> ProfilesDictionary:
        return ProfilesDictionary(
            string_table=list(self.strings),
            function_table=self.function_table,
            location_table=self.location_table,
            stack_table=self.stack_table,
        )


class Profiler:
    """Continuous in-process profiler exporting OTLP profiles.

    Samples the stacks of every thread through CPython's remote debugging API and
    periodically exports the aggregated samples as an OTLP ``ExportProfilesServiceRequest``
    over HTTP/protobuf, with one profile per enabled type:

    - ``cpu``: threads running Python code or native code on CPU.
    - ``wall``: every thread with a Python stack, including those blocked on I/O or locks.
    """

    def __init__(
        self,
        resource: Resource,
        endpoint: str,
        headers: Mapping[str, str],
        interval: float,
        export_interval: float,
        profile_types: tuple[ProfileType, ...] = PROFILE_TYPES,
    ) -> None:
        self.resource = resource
        self.profile_types = profile_types
        self.endpoint = endpoint
        self.headers = {**headers, "Content-Type": "application/x-protobuf"}
        self.interval = interval
        self.export_interval = export_interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="otel-profiler", daemon=True)

    @property
    def is_alive(self) -> bool:
        """Whether the sampling thread is running."""
        return self._thread.is_alive()

    def start(self) -> None:
        """Start the sampling thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop the sampling thread and export the remaining samples."""
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        unwinder = _remote_debugging.RemoteUnwinder(
            os.getpid(),
            all_threads=True,
            mode=PROFILING_MODE_WALL,
            skip_non_matching_threads=False,
            cache_frames=True,
        )
        own_thread = threading.get_native_id()
        samples = self._new_samples()
        window_start = time.time_ns()

        while not self._stop.wait(self.interval):
            try:
                _sample(unwinder, own_thread, samples)
            except RuntimeError, OSError:
                logger.debug("Profiler sampling failed", exc_info=True)

            now = time.time_ns()
            if now - window_start >= self.export_interval * _NANOS_PER_SECOND:
                self.export(samples, window_start, now)
                samples = self._new_samples()
                window_start = now

        self.export(samples, window_start, time.time_ns())

    def _new_samples(self) -> Samples:
        return {profile_type: Counter() for profile_type in self.profile_types}

    def export(self, samples: Samples, start: int, end: int) -> None:
        """Send the aggregated samples of a time window, logging failures instead of raising."""
        if not any(samples.values()):
            return

        try:
            body = self.build_request(samples, start, end).SerializeToString()
            request = urllib.request.Request(  # noqa: S310
                self.endpoint, data=body, headers=self.headers, method="POST"
            )
            with suppress_instrumentation(), urllib.request.urlopen(request, timeout=10):  # noqa: S310
                pass
        except Exception:
            logger.warning("Profiler export failed", exc_info=True)

    def build_request(self, samples: Samples, start: int, end: int) -> ExportProfilesServiceRequest:
        """Build the OTLP export request for the samples collected between ``start`` and ``end``."""
        dictionary = _Dictionary()
        period = int(self.interval * _NANOS_PER_SECOND)
        profiles = [
            Profile(
                sample_type=ValueType(
                    type_strindex=dictionary.string(profile_type),
                    unit_strindex=dictionary.string("nanoseconds"),
                ),
                period_type=ValueType(
                    type_strindex=dictionary.string(profile_type),
                    unit_strindex=dictionary.string("nanoseconds"),
                ),
                period=period,
                time_unix_nano=start,
                duration_nano=end - start,
                profile_id=os.urandom(16),
                samples=[
                    Sample(stack_index=dictionary.stack(key), values=[count * period])
                    for key, count in counter.items()
                ],
            )
            for profile_type, counter in samples.items()
            if counter
        ]
        return ExportProfilesServiceRequest(
            resource_profiles=[
                ResourceProfiles(
                    resource=self.resource,
                    scope_profiles=[
                        ScopeProfiles(scope=InstrumentationScope(name=_SCOPE_NAME), profiles=profiles),
                    ],
                ),
            ],
            dictionary=dictionary.to_proto(),
        )


def _sample(unwinder: _remote_debugging.RemoteUnwinder, own_thread: int, samples: Samples) -> None:
    cpu, wall = samples.get("cpu"), samples.get("wall")
    for interpreter in unwinder.get_stack_trace():
        for thread in interpreter.threads:
            if thread.thread_id == own_thread or not thread.frame_info:
                continue

            key = tuple(_frame(frame) for frame in thread.frame_info)
            if wall is not None:
                wall[key] += 1

            if cpu is not None and thread.status & _RUNNING:
                cpu[key] += 1


def _frame(frame: _remote_debugging.FrameInfo | tuple[object, ...]) -> Frame:
    filename, location, funcname, *_ = frame
    lineno = location[0] if isinstance(location, tuple) else 0
    return str(filename), str(funcname), int(lineno)


def to_proto_resource(attributes: Mapping[str, AttributeValue]) -> Resource:
    """Convert OpenTelemetry resource attributes to the OTLP ``Resource`` message."""
    return Resource(
        attributes=[KeyValue(key=key, value=_any_value(value)) for key, value in attributes.items()]
    )


def _any_value(value: AttributeValue) -> AnyValue:
    match value:
        case bool():
            return AnyValue(bool_value=value)
        case int():
            return AnyValue(int_value=value)
        case float():
            return AnyValue(double_value=value)
        case str():
            return AnyValue(string_value=value)
        case _:
            return AnyValue(string_value=str(value))
