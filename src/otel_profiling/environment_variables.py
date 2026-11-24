OTEL_PROFILES_EXPORTER = "OTEL_PROFILES_EXPORTER"
"""
.. envvar:: OTEL_PROFILES_EXPORTER

Selects the profiles exporter. The profiler only starts when set to ``otlp``.
"""

OTEL_EXPORTER_OTLP_PROFILES_ENDPOINT = "OTEL_EXPORTER_OTLP_PROFILES_ENDPOINT"
"""
.. envvar:: OTEL_EXPORTER_OTLP_PROFILES_ENDPOINT

Full URL the profiles are sent to. Defaults to ``OTEL_EXPORTER_OTLP_ENDPOINT`` with
``/v1/profiles`` appended.
"""

OTEL_EXPORTER_OTLP_PROFILES_HEADERS = "OTEL_EXPORTER_OTLP_PROFILES_HEADERS"
"""
.. envvar:: OTEL_EXPORTER_OTLP_PROFILES_HEADERS

Headers sent with each export, as ``key=value`` pairs separated by commas. Defaults to
``OTEL_EXPORTER_OTLP_HEADERS``.
"""

OTEL_PYTHON_PROFILING_SAMPLE_INTERVAL = "OTEL_PYTHON_PROFILING_SAMPLE_INTERVAL"
"""
.. envvar:: OTEL_PYTHON_PROFILING_SAMPLE_INTERVAL

Milliseconds between stack samples. Default: ``10``.
"""

OTEL_PYTHON_PROFILING_EXPORT_INTERVAL = "OTEL_PYTHON_PROFILING_EXPORT_INTERVAL"
"""
.. envvar:: OTEL_PYTHON_PROFILING_EXPORT_INTERVAL

Milliseconds between profile exports. Default: ``10000``.
"""

OTEL_PYTHON_PROFILING_TYPES = "OTEL_PYTHON_PROFILING_TYPES"
"""
.. envvar:: OTEL_PYTHON_PROFILING_TYPES

Comma-separated profile types to collect: ``cpu`` (threads running on CPU) and/or ``wall``
(every thread, including those blocked on I/O or locks). Default: ``cpu,wall``.
"""

OTEL_PYTHON_PROFILING_SPAN_LINKS = "OTEL_PYTHON_PROFILING_SPAN_LINKS"
"""
.. envvar:: OTEL_PYTHON_PROFILING_SPAN_LINKS

Whether to link each sample to the span active on its thread or asyncio task (``trace_id`` /
``span_id``). Requires an SDK tracer provider. Default: ``true``.
"""

OTEL_PYTHON_PROFILING_MODE = "OTEL_PYTHON_PROFILING_MODE"
"""
.. envvar:: OTEL_PYTHON_PROFILING_MODE

Where samples are taken: ``process`` (default) starts a child interpreter that reads this
process' memory without competing for its GIL; ``thread`` samples from a background thread.
``process`` falls back to ``thread`` when the child can't read this process' memory.
"""
