import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

from otel_profiling.span_tracker import SpanTracker, _TracedCoroutine


def _tracer(tracker: SpanTracker, **kwargs: object):
    provider = TracerProvider(**kwargs)  # type: ignore[arg-type]
    provider.add_span_processor(tracker)  # type: ignore[arg-type]
    return provider.get_tracer(__name__)


def _link(span) -> tuple[int, int]:
    context = span.get_span_context()
    return context.trace_id, context.span_id


def test_tracks_nested_spans_per_thread() -> None:
    """The innermost open span is the active one, and its parent is restored when it ends."""
    tracker = SpanTracker()
    tracer = _tracer(tracker)
    thread_id = threading.get_native_id()

    with tracer.start_as_current_span("outer") as outer:
        with tracer.start_as_current_span("inner") as inner:
            assert tracker.current(thread_id) == _link(inner)

        assert tracker.current(thread_id) == _link(outer)

    assert tracker.current(thread_id) is None


def test_spans_are_isolated_per_thread() -> None:
    """A span opened in one thread is not active in another."""
    tracker = SpanTracker()
    tracer = _tracer(tracker)
    seen: list[object] = []

    with tracer.start_as_current_span("main"):
        thread = threading.Thread(target=lambda: seen.append(tracker.current(threading.get_native_id())))
        thread.start()
        thread.join()

    assert seen == [None]


def test_ignores_unsampled_spans() -> None:
    """Spans that will not be exported are not linked."""
    tracker = SpanTracker()
    tracer = _tracer(tracker, sampler=ALWAYS_OFF)

    with tracer.start_as_current_span("dropped"):
        assert tracker.current(threading.get_native_id()) is None


def test_disabled_tracker_ignores_spans() -> None:
    """A disabled tracker does not record new spans."""
    tracker = SpanTracker()
    tracker.enabled = False
    tracer = _tracer(tracker)

    with tracer.start_as_current_span("ignored"):
        assert tracker.current(threading.get_native_id()) is None


def test_asyncio_tasks_publish_their_own_span() -> None:
    """Each asyncio task step publishes the span of its own context, even when tasks interleave."""
    tracker = SpanTracker()
    tracer = _tracer(tracker)
    observed: dict[str, list[object]] = {"a": [], "b": []}
    expected: dict[str, tuple[int, int]] = {}

    async def handler(name: str) -> None:
        with tracer.start_as_current_span(name) as span:
            expected[name] = _link(span)
            for _ in range(3):
                observed[name].append(tracker.current(threading.get_native_id()))
                await asyncio.sleep(0)

    async def main() -> None:
        with tracer.start_as_current_span("bootstrap"):
            pass

        await asyncio.gather(asyncio.create_task(handler("a")), asyncio.create_task(handler("b")))

    asyncio.run(main())

    assert observed["a"] == [expected["a"]] * 3
    assert observed["b"] == [expected["b"]] * 3
    assert tracker.current(threading.get_native_id()) is None


def test_task_factory_wraps_coroutines_once() -> None:
    """Tasks created after the first span in a loop are wrapped, and their result is preserved."""
    tracker = SpanTracker()
    tracer = _tracer(tracker)

    async def answer() -> int:
        return 42

    async def main() -> tuple[bool, int]:
        with tracer.start_as_current_span("install"):
            pass

        task = asyncio.create_task(answer())
        return isinstance(task.get_coro(), _TracedCoroutine), await task

    assert asyncio.run(main()) == (True, 42)


def test_task_factory_chains_existing_factory() -> None:
    """A task factory already set on the loop keeps being used."""
    tracker = SpanTracker()
    tracer = _tracer(tracker)
    created: list[type] = []

    def factory(loop: asyncio.AbstractEventLoop, coro, **kwargs):
        created.append(type(coro))
        return asyncio.Task(coro, loop=loop, **kwargs)

    async def noop() -> None:
        pass

    async def main() -> None:
        asyncio.get_running_loop().set_task_factory(factory)
        with tracer.start_as_current_span("install"):
            pass

        await asyncio.create_task(noop())

    asyncio.run(main())

    assert _TracedCoroutine in created


def test_exceptions_propagate_through_wrapped_tasks() -> None:
    """Exceptions raised inside wrapped tasks reach the awaiting code."""
    tracker = SpanTracker()
    tracer = _tracer(tracker)

    async def fail() -> None:
        await asyncio.sleep(0)
        raise ValueError

    async def main() -> str:
        with tracer.start_as_current_span("install"):
            pass

        try:
            await asyncio.create_task(fail())
        except ValueError:
            return "raised"

        return "missing"

    assert asyncio.run(main()) == "raised"


def test_executor_work_is_linked_to_submitting_span() -> None:
    """Work submitted to a thread pool runs linked to the span active where it was submitted."""
    tracker = SpanTracker()
    tracer = _tracer(tracker)

    with ThreadPoolExecutor(1) as executor, tracer.start_as_current_span("request") as span:
        seen = executor.submit(lambda: tracker.current(threading.get_native_id())).result()

    assert seen == _link(span)


def test_run_in_executor_from_asyncio_is_linked() -> None:
    """``loop.run_in_executor`` work started inside a span is linked to it."""
    tracker = SpanTracker()
    tracer = _tracer(tracker)

    async def main() -> tuple[object, tuple[int, int]]:
        with tracer.start_as_current_span("request") as span:
            loop = asyncio.get_running_loop()
            seen = await loop.run_in_executor(None, lambda: tracker.current(threading.get_native_id()))
            return seen, _link(span)

    seen, expected = asyncio.run(main())

    assert seen == expected
