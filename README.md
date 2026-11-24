# otel-profiling

Continuous in-process CPU profiler for Python that exports [OpenTelemetry profiles](https://opentelemetry.io/docs/specs/otel/profiles/) over OTLP.

It samples the stacks of every running thread with CPython's built-in remote debugging API (the same one behind
`python -m profiling.sampling`), so it needs **no extra privileges** (no `SYS_PTRACE`, no eBPF) and **no native
dependencies**. It runs inside your process, which makes it a fit for environments where you can't deploy a
profiling agent or a privileged sidecar.

> **Status: experimental.** The OTLP profiles signal is still `v1development`, and the CPython API used for
> sampling is private (`_remote_debugging`). Expect breaking changes between Python minor versions.

## Requirements

- Python **3.15+**
- A backend that accepts OTLP profiles over HTTP/protobuf (e.g. the OpenTelemetry Collector with the profiles
  feature gate, OpenObserve, Grafana Pyroscope)

## Installation

```bash
pip install otel-profiling
```

## Usage

The package registers an `opentelemetry_instrumentor` entry point, so with zero-code instrumentation it is picked up
automatically:

```bash
export OTEL_PROFILES_EXPORTER=otlp
opentelemetry-instrument python app.py
```

Or enable it manually, after configuring your tracer provider:

```python
from otel_profiling import ProfilingInstrumentor

ProfilingInstrumentor().instrument()
```

The profiler only starts when `OTEL_PROFILES_EXPORTER=otlp`, so it is safe to call `instrument()` unconditionally.
Profiles carry the resource of the tracer provider (`service.name`, `service.instance.id`, ...), so they line up
with your traces in the backend.

### Keyword arguments

Arguments passed to `instrument()` take precedence over environment variables.

| Argument | Default |
|---|---|
| `tracer_provider` | The global tracer provider (its resource is attached to the profiles) |
| `endpoint` | `OTEL_EXPORTER_OTLP_PROFILES_ENDPOINT`, or `OTEL_EXPORTER_OTLP_ENDPOINT` + `/v1/profiles` |
| `headers` | `OTEL_EXPORTER_OTLP_PROFILES_HEADERS`, or `OTEL_EXPORTER_OTLP_HEADERS` |
| `sample_interval_millis` | `OTEL_PYTHON_PROFILING_SAMPLE_INTERVAL`, or `10` |
| `export_interval_millis` | `OTEL_PYTHON_PROFILING_EXPORT_INTERVAL`, or `10000` |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OTEL_PROFILES_EXPORTER` | — | Set to `otlp` to enable the profiler |
| `OTEL_EXPORTER_OTLP_PROFILES_ENDPOINT` | `OTEL_EXPORTER_OTLP_ENDPOINT` + `/v1/profiles` | Full URL profiles are sent to |
| `OTEL_EXPORTER_OTLP_PROFILES_HEADERS` | `OTEL_EXPORTER_OTLP_HEADERS` | Export headers, as `key=value` pairs separated by commas |
| `OTEL_PYTHON_PROFILING_SAMPLE_INTERVAL` | `10` | Milliseconds between stack samples |
| `OTEL_PYTHON_PROFILING_EXPORT_INTERVAL` | `10000` | Milliseconds between exports |

## How it works

- A daemon thread reads the stacks of all threads every sample interval and keeps the ones that are running Python
  code or native code on CPU. Threads idle on I/O or locks are skipped, so the result is a CPU profile.
- Samples are aggregated per stack and sent every export interval as an `ExportProfilesServiceRequest`
  (`cpu` / `nanoseconds`, each sample weighted by the sampling period).
- After `fork()` (e.g. Gunicorn workers, `multiprocessing`) the profiler restarts in the child, and the last window
  is flushed at interpreter exit.

## Limitations

- Samples are not linked to spans yet (`trace_id` / `span_id`).
- asyncio tasks waiting on I/O don't show up, since they are not using CPU; the running task's stack does.
- Only HTTP/protobuf export is supported (no gRPC).
