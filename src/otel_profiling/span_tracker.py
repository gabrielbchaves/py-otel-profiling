import asyncio
import functools
import itertools
import threading
import weakref
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, override

from opentelemetry import trace

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from opentelemetry.context import Context
    from opentelemetry.trace import Span, SpanContext

    from otel_profiling.span_table import SpanTable

type SpanLink = tuple[int, int]

TRACKER_FILE = __file__

_active_tracker: weakref.ReferenceType[SpanTracker] | None = None
_executor_keys = itertools.count(-1, -1)


class SpanTracker:
    """Span processor that publishes the active span of each thread for the profiler.

    Synchronous code keeps a stack of open spans per thread. Code running in an asyncio
    event loop is tracked per task step instead: the first span started in a loop installs
    a task factory that wraps new tasks, so each step publishes the span active in its own
    context while it runs. Work submitted to a ``ThreadPoolExecutor`` (``run_in_executor``,
    asgiref's ``sync_to_async``) runs linked to the span active where it was submitted. Only
    sampled spans are tracked.

    When a ``table`` is set, every change of a thread's active span is also published there,
    so a sampler running in another process can read it.
    """

    def __init__(self, table: SpanTable | None = None) -> None:
        self.enabled = True
        self.table = table
        self._threads: dict[int, dict[int, SpanLink]] = {}
        self._owners: dict[int, int] = {}
        self._running: dict[int, SpanLink] = {}
        self._patched_loops: weakref.WeakSet[asyncio.AbstractEventLoop] = weakref.WeakSet()
        _install_executor_hook(self)

    def current(self, thread_id: int) -> SpanLink | None:
        """Return the span active on the given native thread, if any."""
        if link := self._running.get(thread_id):
            return link

        if spans := self._threads.get(thread_id):
            return next(reversed(spans.values()), None)

        return None

    def clear(self) -> None:
        """Forget every tracked span."""
        self._threads.clear()
        self._owners.clear()
        self._running.clear()
        if self.table:
            self.table.clear()

    def push(self, thread_id: int, key: int, link: SpanLink) -> None:
        """Mark ``link`` as the innermost span of a thread until ``pop`` is called with ``key``."""
        self._threads.setdefault(thread_id, {})[key] = link
        self._publish(thread_id)

    def pop(self, thread_id: int, key: int) -> None:
        """Undo a ``push``."""
        spans = self._threads.get(thread_id)
        if spans is not None:
            spans.pop(key, None)
            if not spans:
                del self._threads[thread_id]

        self._publish(thread_id)

    def set_running(self, thread_id: int, link: SpanLink | None) -> None:
        """Set or clear the span of the asyncio task step running on a thread."""
        if link is None:
            self._running.pop(thread_id, None)
        else:
            self._running[thread_id] = link

        self._publish(thread_id)

    def _publish(self, thread_id: int) -> None:
        if self.table:
            self.table.set(thread_id, self.current(thread_id))

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:  # noqa: ARG002
        """Track a span as active on the current thread or asyncio task step."""
        context = span.get_span_context()
        if not self.enabled or not context.trace_flags.sampled:
            return

        thread_id = threading.get_native_id()
        link = (context.trace_id, context.span_id)
        loop = asyncio.events._get_running_loop()  # noqa: SLF001
        if loop is not None:
            self._patch_loop(loop)
            self.set_running(thread_id, link)
            return

        self._owners[context.span_id] = thread_id
        self.push(thread_id, context.span_id, link)

    def _on_ending(self, span: Any) -> None:
        pass

    def on_end(self, span: Any) -> None:
        """Stop tracking a span, restoring its parent as the active span of an asyncio task step."""
        context = span.get_span_context()
        if thread_id := self._owners.pop(context.span_id, None):
            self.pop(thread_id, context.span_id)
            return

        thread_id = threading.get_native_id()
        if self._running.get(thread_id) == (context.trace_id, context.span_id):
            parent = span.parent
            if parent is not None and parent.is_valid and parent.trace_flags.sampled:
                self.set_running(thread_id, (parent.trace_id, parent.span_id))
            else:
                self.set_running(thread_id, None)

    def shutdown(self) -> None:
        """Stop tracking spans."""
        self.enabled = False
        self.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        """Nothing is buffered, so flushing always succeeds."""
        return True

    def _patch_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if loop in self._patched_loops:
            return

        self._patched_loops.add(loop)
        previous = loop.get_task_factory()
        tracker = self

        def factory(loop: asyncio.AbstractEventLoop, coro: Any, **kwargs: Any) -> asyncio.Future[Any]:
            if tracker.enabled and asyncio.iscoroutine(coro) and not isinstance(coro, _TracedCoroutine):
                coro = _TracedCoroutine(coro, tracker)

            if previous is not None:
                return previous(loop, coro, **kwargs)

            return asyncio.Task(coro, loop=loop, **kwargs)

        loop.set_task_factory(factory)


class _TracedCoroutine(Coroutine[Any, Any, Any]):
    __slots__ = ("_coro", "_tracker")

    def __init__(self, coro: Coroutine[Any, Any, Any], tracker: SpanTracker) -> None:
        self._coro = coro
        self._tracker = tracker

    @override
    def send(self, value: Any) -> Any:
        return self._step(self._coro.send, value)

    @override
    def throw(self, *args: Any) -> Any:
        return self._step(self._coro.throw, *args)

    @override
    def close(self) -> None:
        self._coro.close()

    @override
    def __await__(self) -> Generator[Any, None, Any]:
        return self._coro.__await__()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._coro, name)

    def _step(self, method: Callable[..., Any], *args: Any) -> Any:
        thread_id = threading.get_native_id()
        context: SpanContext = trace.get_current_span().get_span_context()
        if context.is_valid and context.trace_flags.sampled:
            self._tracker.set_running(thread_id, (context.trace_id, context.span_id))

        try:
            return method(*args)
        finally:
            self._tracker.set_running(thread_id, None)


def _install_executor_hook(tracker: SpanTracker) -> None:
    global _active_tracker  # noqa: PLW0603
    first = _active_tracker is None
    _active_tracker = weakref.ref(tracker)
    if not first:
        return

    submit = ThreadPoolExecutor.submit

    def linked_submit(self: ThreadPoolExecutor, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        active = _active_tracker() if _active_tracker else None
        context = trace.get_current_span().get_span_context()
        if active is not None and active.enabled and context.is_valid and context.trace_flags.sampled:
            fn = functools.partial(_run_linked, active, (context.trace_id, context.span_id), fn)

        return submit(self, fn, *args, **kwargs)

    linked_submit.__doc__ = submit.__doc__
    ThreadPoolExecutor.submit = linked_submit  # type: ignore[method-assign,assignment]


def _run_linked(
    tracker: SpanTracker, link: SpanLink, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
) -> Any:
    thread_id = threading.get_native_id()
    key = next(_executor_keys)
    tracker.push(thread_id, key, link)
    try:
        return fn(*args, **kwargs)
    finally:
        tracker.pop(thread_id, key)
