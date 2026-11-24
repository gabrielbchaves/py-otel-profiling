# otel-profiling

Continuous CPU and wall-clock profiler for Python that exports [OpenTelemetry profiles](https://opentelemetry.io/docs/specs/otel/profiles/) over OTLP, with each sample linked to the active trace and span.

It samples the stacks of every thread with CPython's built-in remote debugging API (the same one behind
`python -m profiling.sampling`), so it needs **no extra privileges** (no `SYS_PTRACE`, no eBPF) and **no native
dependencies**. It is started by your application itself, which makes it a fit for environments where you can't
deploy a profiling agent or a privileged sidecar.

> **Status: experimental.** The OTLP profiles signal is still `v1development`, and the CPython API used for
> sampling is private (`_remote_debugging`). Expect breaking changes between Python minor versions.

## Requirements

- Python **3.15+**
- Linux for the default `process` mode (other platforms fall back to `thread` mode, see [Modes](#modes))
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
Profiles carry the resource of the tracer provider (`service.name`, `service.instance.id`, ...), and every sample
taken while a span is active carries its `trace_id` / `span_id`, so profiles line up with your traces in the backend.

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
| `span_links` | `OTEL_PYTHON_PROFILING_SPAN_LINKS`, or `True` |
| `mode` | `OTEL_PYTHON_PROFILING_MODE`, or `"process"` |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OTEL_PROFILES_EXPORTER` | — | Set to `otlp` to enable the profiler |
| `OTEL_EXPORTER_OTLP_PROFILES_ENDPOINT` | `OTEL_EXPORTER_OTLP_ENDPOINT` + `/v1/profiles` | Full URL profiles are sent to |
| `OTEL_EXPORTER_OTLP_PROFILES_HEADERS` | `OTEL_EXPORTER_OTLP_HEADERS` | Export headers, as `key=value` pairs separated by commas |
| `OTEL_PYTHON_PROFILING_SAMPLE_INTERVAL` | `10` | Milliseconds between stack samples |
| `OTEL_PYTHON_PROFILING_EXPORT_INTERVAL` | `10000` | Milliseconds between exports |
| `OTEL_PYTHON_PROFILING_TYPES` | `cpu,wall` | Comma-separated profile types to collect (see below) |
| `OTEL_PYTHON_PROFILING_SPAN_LINKS` | `true` | Link samples to the active span; requires an SDK tracer provider |
| `OTEL_PYTHON_PROFILING_MODE` | `process` | Where samples are taken: `process` or `thread` (see below) |

## Profile types

| Type | Value | Use it to find |
|---|---|---|
| `cpu` | CPU time each thread consumed since the previous sample, from its per-thread CPU clock | Hot code paths burning CPU |
| `wall` | Sampling period for every thread with a Python stack, including those blocked on I/O, locks or `sleep` | Where requests spend time waiting (HTTP calls, database, LLM APIs) |

Both come from the same stack sample, so enabling both costs no extra sampling. Each export carries one profile per
enabled type, sharing the same dictionary.

## Modes

### `process` (default, Linux)

The instrumentor starts a small sampler process: a fresh interpreter in isolated mode (`python -I`), **not a fork of
your application**, so it doesn't copy its memory or run its auto-instrumentation. Your process allows that child to
read its memory with `prctl(PR_SET_PTRACER)`, which needs no capability. The child samples your process, reads
per-thread CPU time from `/proc/<pid>/task/<tid>/schedstat`, and exports the profiles itself. It exits when your
process stops the profiler or dies.

Because it doesn't compete for your process' GIL, it sees short CPU bursts between I/O waits and asyncio task steps
that an in-process sampler misses. It costs one extra process per instrumented process (about 35 MB RSS) and its own
CPU for sampling.

If the child can't be started or can't read your process' memory (e.g. a seccomp profile blocking
`process_vm_readv`), the profiler logs a warning and falls back to `thread` mode.

### `thread`

Samples from a background thread inside your process. It works anywhere the remote debugging API does, but the
sampler needs the GIL to run, so it can only sample when your threads release it (I/O or the 5 ms switch interval):
CPU bursts shorter than that between I/O waits are under-represented or invisible, and a busy asyncio loop can starve
the sampler entirely. Use it where `process` mode isn't available. On platforms without per-thread CPU clocks
(e.g. macOS) only the `wall` profile is produced.

## Span links

With an SDK tracer provider, the instrumentor registers a span processor that tracks the active span of each thread:

- **Threads:** the innermost open span of each thread.
- **asyncio:** the first span started in an event loop installs a task factory on that loop (chaining any existing
  one) that wraps new tasks, so each task step publishes the span of its own context while it runs.
- **Thread pools:** `concurrent.futures.ThreadPoolExecutor.submit` is patched so submitted work runs linked to the
  span active where it was submitted. This covers `loop.run_in_executor` and asgiref's `sync_to_async` (Django).

In `process` mode the active spans are shared with the sampler through a table in shared memory. Only sampled spans
are linked. Set `OTEL_PYTHON_PROFILING_SPAN_LINKS=false` to disable the span processor and the patches.

## How it works

- Every sample interval the sampler reads the stacks of all threads. Each thread adds the sampling period to `wall`
  and the CPU time it consumed since the previous sample to `cpu`, both under its current stack and span.
- Samples are aggregated per stack and span and sent every export interval as an `ExportProfilesServiceRequest`
  (`cpu` / `wall` in `nanoseconds`).
- After `fork()` (e.g. Gunicorn workers, `multiprocessing`) the profiler restarts in the child, and the last window
  is flushed at interpreter exit.

## Limitations

- `process` mode reads the target without pausing it, so some samples are torn (the stack changes while it is read)
  and dropped; code whose stack changes very fast may be slightly under-represented.
- asyncio tasks created before the first span in a loop (e.g. the connection task of the very first request) are not
  wrapped, so their steps are only linked through spans started inside them.
- Suspended asyncio tasks don't show up in either profile: stacks are per thread, so only the running task is seen.
- Only HTTP/protobuf export is supported (no gRPC).
