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
