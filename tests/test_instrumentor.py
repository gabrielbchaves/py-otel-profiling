import asyncio
import os
import threading
import time

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from otel_profiling import ProfilingInstrumentor
from otel_profiling.instrumentor import _endpoint, _headers, _profile_types

from .conftest import OTLPCollector


def burn_cpu(seconds: float) -> None:
    """Keep the CPU busy in pure Python for the given time."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        sum(i * i for i in range(1000))


def test_disabled_without_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The profiler does not start unless OTEL_PROFILES_EXPORTER=otlp."""
    monkeypatch.delenv("OTEL_PROFILES_EXPORTER", raising=False)
    instrumentor = ProfilingInstrumentor()

    instrumentor.instrument()

    assert instrumentor.profiler is None
    instrumentor.uninstrument()


def test_exports_cpu_profile(instrumentor: ProfilingInstrumentor, collector: OTLPCollector) -> None:
    """CPU-bound code shows up in the exported profiles with the tracer provider resource."""
    provider = TracerProvider(resource=Resource.create({"service.name": "profiled-app"}))
    instrumentor.instrument(tracer_provider=provider, endpoint=collector.endpoint, sample_interval_millis=5)

    burn_cpu(0.5)
    instrumentor.uninstrument()

    _, request = collector.requests[0]
    attributes = {a.key: a.value.string_value for a in request.resource_profiles[0].resource.attributes}
    assert attributes["service.name"] == "profiled-app"
    assert "burn_cpu" in collector.function_names()


def test_exports_periodically(instrumentor: ProfilingInstrumentor, collector: OTLPCollector) -> None:
    """Profiles are sent every export interval while the profiler runs."""
    instrumentor.instrument(endpoint=collector.endpoint, sample_interval_millis=5, export_interval_millis=100)

    burn_cpu(0.5)

    assert len(collector.requests) >= 2


def _sampler(instrumentor: ProfilingInstrumentor):
    return instrumentor.sampler_process or instrumentor.profiler


def test_uses_requested_mode(instrumentor: ProfilingInstrumentor, collector: OTLPCollector) -> None:
    """The profiler runs in the mode selected by OTEL_PYTHON_PROFILING_MODE."""
    instrumentor.instrument(endpoint=collector.endpoint)

    if os.environ["OTEL_PYTHON_PROFILING_MODE"] == "process":
        assert instrumentor.sampler_process is not None
        assert instrumentor.profiler is None
    else:
        assert instrumentor.profiler is not None
        assert instrumentor.sampler_process is None


def test_uninstrument_stops_profiler(instrumentor: ProfilingInstrumentor, collector: OTLPCollector) -> None:
    """Uninstrumenting stops the sampler."""
    instrumentor.instrument(endpoint=collector.endpoint)
    sampler = _sampler(instrumentor)
    assert sampler is not None
    assert sampler.is_alive

    instrumentor.uninstrument()

    assert _sampler(instrumentor) is None
    assert not sampler.is_alive


def test_restarts_in_forked_child(instrumentor: ProfilingInstrumentor, collector: OTLPCollector) -> None:
    """A forked child gets its own running profiler."""
    instrumentor.instrument(endpoint=collector.endpoint)
    read_fd, write_fd = os.pipe()

    parent_sampler = _sampler(instrumentor)
    pid = os.fork()
    if pid == 0:
        sampler = _sampler(instrumentor)
        alive = sampler is not None and sampler.is_alive and sampler is not parent_sampler
        os.write(write_fd, b"1" if alive else b"0")
        instrumentor.uninstrument()
        os._exit(0)

    os.waitpid(pid, 0)
    assert os.read(read_fd, 1) == b"1"
    assert parent_sampler is not None
    assert parent_sampler.is_alive


def test_endpoint_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The profiles endpoint falls back to the generic OTLP endpoint plus /v1/profiles."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROFILES_ENDPOINT", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318/")
    assert _endpoint() == "http://collector:4318/v1/profiles"

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROFILES_ENDPOINT", "http://profiles:4318/custom")
    assert _endpoint() == "http://profiles:4318/custom"


def test_headers_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Profile-specific headers take precedence over the generic OTLP headers."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROFILES_HEADERS", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Basic%20generic")
    assert _headers() == {"authorization": "Basic generic"}

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROFILES_HEADERS", "authorization=Basic%20profiles")
    assert _headers() == {"authorization": "Basic profiles"}


def wait_on_lock(event: threading.Event) -> None:
    """Block until the event is set, without using CPU."""
    event.wait()


def _functions_by_type(collector: OTLPCollector) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for _, request in collector.requests:
        dictionary = request.dictionary
        strings = dictionary.string_table
        for profile in request.resource_profiles[0].scope_profiles[0].profiles:
            names = result.setdefault(strings[profile.sample_type.type_strindex], set())
            for sample in profile.samples:
                for index in dictionary.stack_table[sample.stack_index].location_indices:
                    function = dictionary.function_table[
                        dictionary.location_table[index].lines[0].function_index
                    ]
                    names.add(strings[function.name_strindex])

    return result


def test_wall_includes_blocked_threads(instrumentor: ProfilingInstrumentor, collector: OTLPCollector) -> None:
    """Blocked threads show up in the wall profile but not in the CPU profile."""
    event = threading.Event()
    blocked = threading.Thread(target=wait_on_lock, args=(event,))
    blocked.start()
    instrumentor.instrument(endpoint=collector.endpoint, sample_interval_millis=5)

    burn_cpu(0.5)
    instrumentor.uninstrument()
    event.set()
    blocked.join()

    functions = _functions_by_type(collector)
    assert "wait_on_lock" in functions["wall"]
    assert "wait_on_lock" not in functions["cpu"]
    assert "burn_cpu" in functions["cpu"]
    assert "burn_cpu" in functions["wall"]


def test_profile_types_from_env(
    instrumentor: ProfilingInstrumentor, collector: OTLPCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OTEL_PYTHON_PROFILING_TYPES limits which profiles are exported."""
    monkeypatch.setenv("OTEL_PYTHON_PROFILING_TYPES", "wall")
    instrumentor.instrument(endpoint=collector.endpoint, sample_interval_millis=5)

    burn_cpu(0.3)
    instrumentor.uninstrument()

    assert set(_functions_by_type(collector)) == {"wall"}


def test_profile_types_parsing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Profile types are normalized, unknown ones are ignored with a warning, and the default is both."""
    monkeypatch.delenv("OTEL_PYTHON_PROFILING_TYPES", raising=False)
    assert _profile_types(None) == ("cpu", "wall")

    monkeypatch.setenv("OTEL_PYTHON_PROFILING_TYPES", " WALL , memory")
    assert _profile_types(None) == ("wall",)
    assert "memory" in caplog.text

    assert _profile_types(["cpu"]) == ("cpu",)


def test_disabled_without_profile_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """The profiler does not start when no valid profile type is configured."""
    monkeypatch.setenv("OTEL_PROFILES_EXPORTER", "otlp")
    instrumentor = ProfilingInstrumentor()

    instrumentor.instrument(profile_types=["memory"])

    assert instrumentor.profiler is None
    instrumentor.uninstrument()


def wait_with_timeout(event: threading.Event) -> None:
    """Wake up every few milliseconds without doing any work, like an idle worker pool."""
    condition = threading.Condition()
    with condition:
        while not event.is_set():
            condition.wait(0.002)


def test_cpu_ignores_threads_waking_from_timed_waits(
    instrumentor: ProfilingInstrumentor, collector: OTLPCollector
) -> None:
    """Idle threads polling with timeouts do not show up as CPU time."""
    event = threading.Event()
    idle = threading.Thread(target=wait_with_timeout, args=(event,))
    idle.start()
    instrumentor.instrument(endpoint=collector.endpoint, sample_interval_millis=5)

    burn_cpu(0.5)
    instrumentor.uninstrument()
    event.set()
    idle.join()

    cpu = _values_by_function(collector, "cpu")
    assert cpu["burn_cpu"] > 20 * cpu.get("wait_with_timeout", 0)


def _values_by_function(collector: OTLPCollector, profile_type: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for _, request in collector.requests:
        dictionary = request.dictionary
        strings = dictionary.string_table
        for profile in request.resource_profiles[0].scope_profiles[0].profiles:
            if strings[profile.sample_type.type_strindex] != profile_type:
                continue

            for sample in profile.samples:
                names = set()
                for index in dictionary.stack_table[sample.stack_index].location_indices:
                    function = dictionary.function_table[
                        dictionary.location_table[index].lines[0].function_index
                    ]
                    names.add(strings[function.name_strindex])

                for name in names:
                    totals[name] = totals.get(name, 0) + sample.values[0]

    return totals


def _span_ids_by_function(collector: OTLPCollector, function_name: str) -> set[int]:
    span_ids: set[int] = set()
    for _, request in collector.requests:
        dictionary = request.dictionary
        strings = dictionary.string_table
        for profile in request.resource_profiles[0].scope_profiles[0].profiles:
            for sample in profile.samples:
                for index in dictionary.stack_table[sample.stack_index].location_indices:
                    function = dictionary.function_table[
                        dictionary.location_table[index].lines[0].function_index
                    ]
                    if strings[function.name_strindex] == function_name:
                        span_ids.add(int.from_bytes(dictionary.link_table[sample.link_index].span_id))

    return span_ids


def test_links_thread_samples_to_active_span(
    instrumentor: ProfilingInstrumentor, collector: OTLPCollector
) -> None:
    """Samples taken while a span is open on a thread are linked to that span."""
    provider = TracerProvider()
    tracer = provider.get_tracer(__name__)
    instrumentor.instrument(tracer_provider=provider, endpoint=collector.endpoint, sample_interval_millis=5)

    with tracer.start_as_current_span("request") as span:
        burn_cpu(0.3)

    instrumentor.uninstrument()

    assert _span_ids_by_function(collector, "burn_cpu") == {span.get_span_context().span_id}


async def handle_request(tracer, name: str, spans: dict[str, int]) -> None:
    """Simulate an async request handler interleaving CPU work with short I/O waits."""
    with tracer.start_as_current_span(name) as span:
        spans[name] = span.get_span_context().span_id
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            burn_cpu(0.001)
            await asyncio.sleep(0.003)


def test_links_asyncio_samples_to_task_span(
    instrumentor: ProfilingInstrumentor, collector: OTLPCollector
) -> None:
    """Interleaved asyncio tasks are linked to the span of the task running at sample time.

    Only in ``process`` mode: a sampler thread rarely gets the GIL while short task steps run.
    """
    if os.environ["OTEL_PYTHON_PROFILING_MODE"] == "thread":
        pytest.skip("in-process sampling can't observe short asyncio task steps")

    provider = TracerProvider()
    tracer = provider.get_tracer(__name__)
    instrumentor.instrument(tracer_provider=provider, endpoint=collector.endpoint, sample_interval_millis=5)
    spans: dict[str, int] = {}

    async def main() -> None:
        with tracer.start_as_current_span("startup"):
            pass

        await asyncio.gather(
            asyncio.create_task(handle_request(tracer, "a", spans)),
            asyncio.create_task(handle_request(tracer, "b", spans)),
        )

    asyncio.run(main())
    instrumentor.uninstrument()

    assert _span_ids_by_function(collector, "handle_request") - {0} == set(spans.values())
    assert "_step" not in _values_by_function(collector, "wall")


def test_span_links_can_be_disabled(
    instrumentor: ProfilingInstrumentor, collector: OTLPCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OTEL_PYTHON_PROFILING_SPAN_LINKS=false exports samples without links."""
    monkeypatch.setenv("OTEL_PYTHON_PROFILING_SPAN_LINKS", "false")
    provider = TracerProvider()
    tracer = provider.get_tracer(__name__)
    instrumentor.instrument(tracer_provider=provider, endpoint=collector.endpoint, sample_interval_millis=5)

    with tracer.start_as_current_span("request"):
        burn_cpu(0.2)

    instrumentor.uninstrument()

    assert _span_ids_by_function(collector, "burn_cpu") == {0}


def test_falls_back_to_thread_mode(monkeypatch: pytest.MonkeyPatch, collector: OTLPCollector) -> None:
    """When the sampler process can't start, profiling continues in a background thread."""
    monkeypatch.setenv("OTEL_PROFILES_EXPORTER", "otlp")
    monkeypatch.setattr(
        "otel_profiling.sampler_process._CHILD_ENTRYPOINT", "import sys; print('nope'); {path}"
    )
    instrumentor = ProfilingInstrumentor()

    instrumentor.instrument(endpoint=collector.endpoint, mode="process")

    assert instrumentor.sampler_process is None
    assert instrumentor.profiler is not None
    instrumentor.uninstrument()


def test_process_mode_sees_short_cpu_bursts(
    monkeypatch: pytest.MonkeyPatch, collector: OTLPCollector
) -> None:
    """Short CPU bursts between I/O waits are visible from the sampler process."""
    monkeypatch.setenv("OTEL_PROFILES_EXPORTER", "otlp")
    instrumentor = ProfilingInstrumentor()
    instrumentor.instrument(endpoint=collector.endpoint, mode="process", sample_interval_millis=3)

    end = time.monotonic() + 1
    while time.monotonic() < end:
        burn_cpu(0.001)
        time.sleep(0.003)

    instrumentor.uninstrument()

    wall = _values_by_function(collector, "wall")
    test_total = wall["test_process_mode_sees_short_cpu_bursts"]
    assert wall.get("burn_cpu", 0) > 0.05 * test_total


def test_process_mode_falls_back_outside_linux(
    monkeypatch: pytest.MonkeyPatch, collector: OTLPCollector
) -> None:
    """Process mode is Linux-only, so other platforms profile in-process."""
    monkeypatch.setenv("OTEL_PROFILES_EXPORTER", "otlp")
    monkeypatch.setattr("otel_profiling.sampler_process.sys.platform", "darwin")
    instrumentor = ProfilingInstrumentor()

    instrumentor.instrument(endpoint=collector.endpoint, mode="process")

    assert instrumentor.sampler_process is None
    assert instrumentor.profiler is not None
    instrumentor.uninstrument()


def test_thread_mode_without_cpu_clock_still_profiles_wall(
    monkeypatch: pytest.MonkeyPatch, collector: OTLPCollector
) -> None:
    """Without per-thread CPU clocks (e.g. macOS), the wall profile keeps working."""
    monkeypatch.setenv("OTEL_PROFILES_EXPORTER", "otlp")
    monkeypatch.delattr("otel_profiling.profiler.time.pthread_getcpuclockid")
    instrumentor = ProfilingInstrumentor()
    instrumentor.instrument(endpoint=collector.endpoint, mode="thread", sample_interval_millis=5)

    burn_cpu(0.3)
    instrumentor.uninstrument()

    assert "burn_cpu" in _values_by_function(collector, "wall")
    assert "burn_cpu" not in _values_by_function(collector, "cpu")
