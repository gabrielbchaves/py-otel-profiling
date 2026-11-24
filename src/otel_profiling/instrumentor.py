import atexit
import logging
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
    OTEL_PYTHON_PROFILING_MODE,
    OTEL_PYTHON_PROFILING_SAMPLE_INTERVAL,
    OTEL_PYTHON_PROFILING_SPAN_LINKS,
    OTEL_PYTHON_PROFILING_TYPES,
)
from otel_profiling.profiler import PROFILE_TYPES, Profiler, ProfileType, to_proto_resource
from otel_profiling.sampler_process import SamplerProcess, SamplerProcessError, supports_process_mode
from otel_profiling.span_tracker import SpanTracker

if TYPE_CHECKING:
    from collections.abc import Collection

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "http://localhost:4318"


class ProfilingInstrumentor(BaseInstrumentor):  # type: ignore[misc]
    """OpenTelemetry instrumentor that runs a continuous profiler exporting OTLP profiles.

    Enabled only when ``OTEL_PROFILES_EXPORTER=otlp``. Keyword arguments to ``instrument()``
    take precedence over environment variables:

    - ``tracer_provider``: provider whose resource is attached to the profiles
      (default: the global provider).
    - ``endpoint`` / ``headers``: default to ``OTEL_EXPORTER_OTLP_PROFILES_*``, falling back to
      ``OTEL_EXPORTER_OTLP_*``.
    - ``sample_interval_millis``: default ``OTEL_PYTHON_PROFILING_SAMPLE_INTERVAL`` or ``10``.
    - ``export_interval_millis``: default ``OTEL_PYTHON_PROFILING_EXPORT_INTERVAL`` or ``10000``.
    - ``profile_types``: profile types to collect (``cpu``, ``wall``); default
      ``OTEL_PYTHON_PROFILING_TYPES`` or both.
    - ``span_links``: link samples to the active span; default ``OTEL_PYTHON_PROFILING_SPAN_LINKS``
      or ``True``. Requires an SDK tracer provider (one with ``add_span_processor``).
    - ``mode``: ``process`` samples from a child process, ``thread`` from a background thread;
      default ``OTEL_PYTHON_PROFILING_MODE`` or ``process``. ``process`` is Linux-only; on other
      platforms, or when the child can't read this process' memory, it falls back to ``thread``.

    The profiler is restarted in forked children and flushed at interpreter exit.
    """

    _profiler: Profiler | None = None
    _sampler_process: SamplerProcess | None = None
    _mode: str = "process"
    _span_tracker: SpanTracker | None = None
    _span_tracker_provider: object = None
    _config: dict[str, Any]

    @property
    def profiler(self) -> Profiler | None:
        """The in-process profiler, when running in ``thread`` mode."""
        return self._profiler

    @property
    def sampler_process(self) -> SamplerProcess | None:
        """The sampler child process, when running in ``process`` mode."""
        return self._sampler_process

    @override
    def instrumentation_dependencies(self) -> Collection[str]:
        return ()

    @override
    def _instrument(self, **kwargs: Any) -> None:
        profile_types = _profile_types(kwargs.get("profile_types"))
        if os.environ.get(OTEL_PROFILES_EXPORTER) != "otlp" or not profile_types:
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
            "profile_types": profile_types,
            "span_tracker": self._track_spans(tracer_provider, enabled=kwargs.get("span_links")),
        }
        self._mode = (kwargs.get("mode") or os.environ.get(OTEL_PYTHON_PROFILING_MODE) or "process").lower()
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
        if self._span_tracker:
            self._span_tracker.enabled = False
            self._span_tracker.clear()

    def _track_spans(self, tracer_provider: object, *, enabled: bool | None) -> SpanTracker | None:
        if enabled is None:
            enabled = os.environ.get(OTEL_PYTHON_PROFILING_SPAN_LINKS, "true").lower() == "true"

        add_span_processor = getattr(tracer_provider, "add_span_processor", None)
        if not enabled or add_span_processor is None:
            return None

        if self._span_tracker is None or self._span_tracker_provider is not tracer_provider:
            self._span_tracker = SpanTracker()
            self._span_tracker_provider = tracer_provider
            add_span_processor(self._span_tracker)

        self._span_tracker.enabled = True
        return self._span_tracker

    def _start(self) -> None:
        if self._mode == "process" and not supports_process_mode():
            logger.info("Process mode requires Linux, profiling in-process instead")
        elif self._mode == "process":
            try:
                sampler_process = SamplerProcess(**self._config)
                sampler_process.start()
            except (SamplerProcessError, OSError) as error:
                logger.warning("Falling back to in-process profiling: %s", error)
            else:
                self._sampler_process = sampler_process
                return

        self._profiler = Profiler(**self._config)
        self._profiler.start()

    def _stop(self) -> None:
        if self._sampler_process:
            self._sampler_process.stop()
            self._sampler_process = None

        if self._profiler:
            self._profiler.stop()
            self._profiler = None

    def _restart_in_child(self) -> None:
        if self._span_tracker:
            self._span_tracker.clear()

        running = self._profiler is not None or self._sampler_process is not None
        if self._sampler_process:
            self._sampler_process.abandon()
            self._sampler_process = None

        self._profiler = None
        if running:
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


def _profile_types(value: Collection[str] | None) -> tuple[ProfileType, ...]:
    if value is None:
        raw = os.environ.get(OTEL_PYTHON_PROFILING_TYPES)
        value = raw.split(",") if raw else PROFILE_TYPES

    requested = {item.strip().lower() for item in value}
    unknown = requested - set(PROFILE_TYPES)
    if unknown:
        logger.warning("Ignoring unknown profile types: %s", ", ".join(sorted(unknown)))

    return tuple(profile_type for profile_type in PROFILE_TYPES if profile_type in requested)
