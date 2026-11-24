"""Continuous in-process CPU and wall-clock profiler that exports OpenTelemetry (OTLP) profiles."""

from otel_profiling.instrumentor import ProfilingInstrumentor

__all__ = ["ProfilingInstrumentor"]
