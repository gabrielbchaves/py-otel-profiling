"""Continuous CPU and wall-clock profiler that exports OpenTelemetry (OTLP) profiles linked to traces."""

from otel_profiling.instrumentor import ProfilingInstrumentor

__all__ = ["ProfilingInstrumentor"]
