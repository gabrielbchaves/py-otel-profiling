import atexit
import os
import weakref
from typing import TYPE_CHECKING, Any, override

from opentelemetry import trace
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore[attr-defined]
from opentelemetry.util.re import parse_env_headers

from otel_profiling.environment_variables import (
    OTEL_EXPORTER_OTLP_PROFILES_ENDPOINT,
    OTEL_EXPORTER_OTLP_PROFILES_HEADERS,
    OTEL_PROFILES_EXPORTER,
    OTEL_PYTHON_PROFILING_EXPORT_INTERVAL,
    OTEL_PYTHON_PROFILING_SAMPLE_INTERVAL,
)
from otel_profiling.profiler import Profiler, to_proto_resource

if TYPE_CHECKING:
    from collections.abc import Collection

_DEFAULT_ENDPOINT = "http://localhost:4318"


class ProfilingInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """OpenTelemetry instrumentor that runs a continuous CPU profiler exporting OTLP profiles.

    Enabled only when ``OTEL_PROFILES_EXPORTER=otlp``. Keyword arguments to ``instrument()``
    take precedence over environment variables:

    - ``tracer_provider``: provider whose resource is attached to the profiles
      (default: the global provider).
    - ``endpoint`` / ``headers``: default to ``OTEL_EXPORTER_OTLP_PROFILES_*``, falling back to
      ``OTEL_EXPORTER_OTLP_*``.
    - ``sample_interval_millis``: default ``OTEL_PYTHON_PROFILING_SAMPLE_INTERVAL`` or ``10``.
    - ``export_interval_millis``: default ``OTEL_PYTHON_PROFILING_EXPORT_INTERVAL`` or ``10000``.

    The profiler is restarted in forked children and flushed at interpreter exit.
    """

    _profiler: Profiler | None = None
    _config: dict[str, Any]

    @property
    def profiler(self) -> Profiler | None:
        """The running profiler, if any."""
        return self._profiler

    @override
    def instrumentation_dependencies(self) -> Collection[str]:
        return ()

    @override
    def _instrument(self, **kwargs: Any) -> None:
        if os.environ.get(OTEL_PROFILES_EXPORTER) != "otlp":
            return

        tracer_provider = kwargs.get("tracer_provider") or trace.get_tracer_provider()
        resource = getattr(tracer_provider, "resource", None)
        self._config = {
            "resource": to_proto_resource(resource.attributes if resource else {}),
            "endpoint": kwargs.get("endpoint") or _endpoint(),
            "headers": kwargs.get("headers") or _headers(),
            "interval": _millis(
                kwargs.get("sample_interval_millis"), OTEL_PYTHON_PROFILING_SAMPLE_INTERVAL, 10
            ),
            "export_interval": _millis(
                kwargs.get("export_interval_millis"), OTEL_PYTHON_PROFILING_EXPORT_INTERVAL, 10000
            ),
        }
        self._start()
        atexit.register(self._stop)
        if hasattr(os, "register_at_fork"):
            weak_restart = weakref.WeakMethod(self._restart_in_child)

            def _after_in_child() -> None:
                if restart := weak_restart():
                    restart()

            os.register_at_fork(after_in_child=_after_in_child)

    @override
    def _uninstrument(self, **kwargs: Any) -> None:
        self._stop()

    def _start(self) -> None:
        self._profiler = Profiler(**self._config)
        self._profiler.start()

    def _stop(self) -> None:
        if self._profiler:
            self._profiler.stop()
            self._profiler = None

    def _restart_in_child(self) -> None:
        if self._profiler:
            self._start()


def _endpoint() -> str:
    if endpoint := os.environ.get(OTEL_EXPORTER_OTLP_PROFILES_ENDPOINT):
        return endpoint

    return os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", _DEFAULT_ENDPOINT).rstrip("/") + "/v1/profiles"


def _headers() -> dict[str, str]:
    raw = os.environ.get(OTEL_EXPORTER_OTLP_PROFILES_HEADERS) or os.environ.get(
        "OTEL_EXPORTER_OTLP_HEADERS", ""
    )
    return dict(parse_env_headers(raw, liberal=True))


def _millis(value: int | None, env_var: str, default: int) -> float:
    return int(value or os.environ.get(env_var) or default) / 1000
