# otel-profiling

Continuous in-process CPU and wall-clock profiler for Python that exports [OpenTelemetry profiles](https://opentelemetry.io/docs/specs/otel/profiles/) over OTLP.

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
| `profile_types` | `OTEL_PYTHON_PROFILING_TYPES`, or `("cpu", "wall")` |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OTEL_PROFILES_EXPORTER` | — | Set to `otlp` to enable the profiler |
| `OTEL_EXPORTER_OTLP_PROFILES_ENDPOINT` | `OTEL_EXPORTER_OTLP_ENDPOINT` + `/v1/profiles` | Full URL profiles are sent to |
| `OTEL_EXPORTER_OTLP_PROFILES_HEADERS` | `OTEL_EXPORTER_OTLP_HEADERS` | Export headers, as `key=value` pairs separated by commas |
| `OTEL_PYTHON_PROFILING_SAMPLE_INTERVAL` | `10` | Milliseconds between stack samples |
| `OTEL_PYTHON_PROFILING_EXPORT_INTERVAL` | `10000` | Milliseconds between exports |
| `OTEL_PYTHON_PROFILING_TYPES` | `cpu,wall` | Comma-separated profile types to collect (see below) |

## Profile types

| Type | Samples | Use it to find |
|---|---|---|
| `cpu` | Threads running Python code or native code on CPU | Hot code paths burning CPU |
| `wall` | Every thread with a Python stack, including those blocked on I/O, locks or `sleep` | Where requests spend time waiting (HTTP calls, database, LLM APIs) |

Both come from the same stack sample, so enabling both costs no extra sampling. Each export carries one profile per
enabled type, sharing the same dictionary.

## How it works

- A daemon thread reads the stacks of all threads every sample interval. Every thread counts towards `wall`; only
  threads running on CPU, holding the GIL or waiting for it count towards `cpu`. The sampler itself holds the GIL
  while sampling, so a busy Python thread shows up as *waiting for the GIL* and is still counted as CPU.
- Samples are aggregated per stack and sent every export interval as an `ExportProfilesServiceRequest`
  (`cpu` / `wall` in `nanoseconds`, each sample weighted by the sampling period).
- After `fork()` (e.g. Gunicorn workers, `multiprocessing`) the profiler restarts in the child, and the last window
  is flushed at interpreter exit.

## Limitations

- Samples are not linked to spans yet (`trace_id` / `span_id`).
- asyncio tasks are not unwound individually: the stacks are per thread, so suspended tasks don't show up in
  either profile; only the task running at sample time does.
- A thread genuinely contending for the GIL also counts as `cpu`.
- Only HTTP/protobuf export is supported (no gRPC).
