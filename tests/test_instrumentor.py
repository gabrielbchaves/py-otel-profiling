import os
import time

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from otel_profiling import ProfilingInstrumentor
from otel_profiling.instrumentor import _endpoint, _headers

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


def test_uninstrument_stops_profiler(instrumentor: ProfilingInstrumentor, collector: OTLPCollector) -> None:
    """Uninstrumenting stops the sampling thread."""
    instrumentor.instrument(endpoint=collector.endpoint)
    profiler = instrumentor.profiler
    assert profiler is not None
    assert profiler.is_alive

    instrumentor.uninstrument()

    assert instrumentor.profiler is None
    assert not profiler.is_alive


def test_restarts_in_forked_child(instrumentor: ProfilingInstrumentor, collector: OTLPCollector) -> None:
    """A forked child gets its own running profiler."""
    instrumentor.instrument(endpoint=collector.endpoint)
    read_fd, write_fd = os.pipe()

    pid = os.fork()
    if pid == 0:
        profiler = instrumentor.profiler
        os.write(write_fd, b"1" if profiler and profiler.is_alive else b"0")
        os._exit(0)

    os.waitpid(pid, 0)
    assert os.read(read_fd, 1) == b"1"


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
