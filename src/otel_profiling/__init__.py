"""Continuous in-process CPU profiler that exports OpenTelemetry (OTLP) profiles."""

from otel_profiling.instrumentor import ProfilingInstrumentor

__all__ = ["ProfilingInstrumentor"]
