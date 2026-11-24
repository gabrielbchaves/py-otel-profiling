import _remote_debugging
import logging
import os
import threading
import time
import urllib.request
from collections import Counter
from profiling.sampling.constants import PROFILING_MODE_WALL
from typing import TYPE_CHECKING, Literal, override

from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.proto.collector.profiles.v1development.profiles_service_pb2 import (
    ExportProfilesServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.profiles.v1development.profiles_pb2 import (
    Function,
    Line,
    Link,
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

from otel_profiling.span_tracker import TRACKER_FILE, SpanLink, SpanTracker

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from opentelemetry.util.types import AttributeValue

    from otel_profiling.span_table import SpanTable

logger = logging.getLogger(__name__)

type Frame = tuple[str, str, int]
type StackKey = tuple[Frame, ...]
type SampleKey = tuple[StackKey, SpanLink | None]
type ProfileType = Literal["cpu", "wall"]
type Samples = dict[ProfileType, Counter[SampleKey]]

PROFILE_TYPES: tuple[ProfileType, ...] = ("cpu", "wall")

_SCOPE_NAME = "otel_profiling"
_NANOS_PER_SECOND = 1_000_000_000


class _Dictionary:
    def __init__(self) -> None:
        self.strings: dict[str, int] = {"": 0}
        self.functions: dict[tuple[str, str], int] = {("", ""): 0}
        self.locations: dict[Frame, int] = {}
        self.stacks: dict[StackKey, int] = {(): 0}
        self.function_table = [Function()]
        self.location_table = [Location()]
        self.stack_table = [Stack()]
        self.links: dict[SpanLink | None, int] = {None: 0}
        self.link_table = [Link()]

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

    def link(self, link: SpanLink | None) -> int:
        if link not in self.links and link is not None:
            trace_id, span_id = link
            self.links[link] = len(self.link_table)
            self.link_table.append(
                Link(trace_id=trace_id.to_bytes(16, "big"), span_id=span_id.to_bytes(8, "big")),
            )

        return self.links[link]

    def to_proto(self) -> ProfilesDictionary:
        return ProfilesDictionary(
            string_table=list(self.strings),
            link_table=self.link_table,
            function_table=self.function_table,
            location_table=self.location_table,
            stack_table=self.stack_table,
        )


class Profiler:
    """Continuous profiler exporting OTLP profiles.

    Samples the stacks of every thread of a Python process through CPython's remote debugging
    API and periodically exports the aggregated samples as an OTLP ``ExportProfilesServiceRequest``
    over HTTP/protobuf, with one profile per enabled type:

    - ``cpu``: CPU time each thread consumed since the previous sample, read from its
      per-thread CPU clock and attributed to its current stack.
    - ``wall``: every thread with a Python stack, including those blocked on I/O or locks.

    By default it samples its own process from a background thread, linking samples through
    ``span_tracker``. With ``pid`` it samples another process instead (see ``otel_profiling._process``),
    reading per-thread CPU time from ``/proc`` and span links from ``span_table``.
    """

    def __init__(
        self,
        resource: Resource,
        endpoint: str,
        headers: Mapping[str, str],
        interval: float,
        export_interval: float,
        profile_types: tuple[ProfileType, ...] = PROFILE_TYPES,
        span_tracker: SpanTracker | None = None,
        *,
        pid: int | None = None,
        span_table: SpanTable | None = None,
    ) -> None:
        self.resource = resource
        self.span_tracker = span_tracker
        self.pid = pid
        self.span_table = span_table
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
        sampler = self._new_sampler()
        samples = self._new_samples()
        window_start = time.time_ns()

        while not self._stop.wait(self.interval):
            try:
                sampler.sample(samples)
            except RuntimeError, OSError:
                logger.debug("Profiler sampling failed", exc_info=True)

            now = time.time_ns()
            if now - window_start >= self.export_interval * _NANOS_PER_SECOND:
                self.export(samples, window_start, now)
                samples = self._new_samples()
                window_start = now

        self.export(samples, window_start, time.time_ns())

    def _new_sampler(self) -> _Sampler:
        period = int(self.interval * _NANOS_PER_SECOND)
        if self.pid is not None:
            return _ProcessSampler(self.pid, period, self.span_table)

        return _ThreadSampler(period, self.span_tracker)

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
                    Sample(
                        stack_index=dictionary.stack(stack),
                        link_index=dictionary.link(link),
                        values=[value],
                    )
                    for (stack, link), value in counter.items()
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


class _Sampler:
    def __init__(self, pid: int, period: int) -> None:
        self.period = period
        self.unwinder = _remote_debugging.RemoteUnwinder(
            pid,
            all_threads=True,
            mode=PROFILING_MODE_WALL,
            skip_non_matching_threads=False,
            cache_frames=True,
        )
        self.cpu_times: dict[int, int] = {}
        self.exclude: int | None = None

    def links(self) -> Callable[[int], SpanLink | None]:
        return lambda _: None

    def cpu_clock(self) -> Callable[[int], int | None]:
        raise NotImplementedError

    def sample(self, samples: Samples) -> None:
        cpu, wall = samples.get("cpu"), samples.get("wall")
        trace = self.unwinder.get_stack_trace()
        link_of = self.links()
        cpu_time_of = self.cpu_clock() if cpu is not None else None
        cpu_times: dict[int, int] = {}
        for interpreter in trace:
            for thread in interpreter.threads:
                if thread.thread_id == self.exclude or not thread.frame_info:
                    continue

                try:
                    stack = tuple(
                        frame for frame in map(_frame, thread.frame_info) if frame[0] != TRACKER_FILE
                    )
                except UnicodeEncodeError:
                    continue

                link = link_of(thread.thread_id)
                if wall is not None:
                    wall[stack, link] += self.period

                if cpu is None or cpu_time_of is None:
                    continue

                cpu_time = cpu_time_of(thread.thread_id)
                if cpu_time is None:
                    continue

                cpu_times[thread.thread_id] = cpu_time
                previous = self.cpu_times.get(thread.thread_id)
                if previous is not None and cpu_time > previous:
                    cpu[stack, link] += cpu_time - previous

        self.cpu_times = cpu_times


class _ThreadSampler(_Sampler):
    def __init__(self, period: int, span_tracker: SpanTracker | None) -> None:
        super().__init__(os.getpid(), period)
        self.span_tracker = span_tracker
        self.exclude = threading.get_native_id()

    @override
    def links(self) -> Callable[[int], SpanLink | None]:
        return self.span_tracker.current if self.span_tracker else super().links()

    @override
    def cpu_clock(self) -> Callable[[int], int | None]:
        if not hasattr(time, "pthread_getcpuclockid"):
            return lambda _: None

        idents = {thread.native_id: thread.ident for thread in threading.enumerate()}

        def cpu_time(thread_id: int) -> int | None:
            ident = idents.get(thread_id)
            if ident is None:
                return None

            try:
                return time.clock_gettime_ns(time.pthread_getcpuclockid(ident))
            except OSError, ProcessLookupError:
                return None

        return cpu_time


class _ProcessSampler(_Sampler):
    def __init__(self, pid: int, period: int, span_table: SpanTable | None) -> None:
        super().__init__(pid, period)
        self.pid = pid
        self.span_table = span_table
        self.ticks_per_second = os.sysconf("SC_CLK_TCK")

    @override
    def links(self) -> Callable[[int], SpanLink | None]:
        return self.span_table.read().get if self.span_table else super().links()

    @override
    def cpu_clock(self) -> Callable[[int], int | None]:
        def cpu_time(thread_id: int) -> int | None:
            try:
                with open(f"/proc/{self.pid}/task/{thread_id}/schedstat", "rb") as schedstat:  # noqa: PTH123
                    return int(schedstat.read().split(maxsplit=1)[0])
            except OSError, ValueError, IndexError:
                return None

        return cpu_time


def _frame(frame: _remote_debugging.FrameInfo | tuple[object, ...]) -> Frame:
    filename, location, funcname, *_ = frame
    lineno = location[0] if isinstance(location, tuple) else 0
    filename, funcname = str(filename), str(funcname)
    filename.encode()
    funcname.encode()
    return filename, funcname, int(lineno)


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
