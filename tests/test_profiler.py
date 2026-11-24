import _remote_debugging
import os
from collections import Counter

from opentelemetry.proto.resource.v1.resource_pb2 import Resource

from otel_profiling.profiler import Profiler, _Dictionary, _frame, to_proto_resource

from .conftest import OTLPCollector


def _profiler(endpoint: str = "http://127.0.0.1:1/v1/profiles") -> Profiler:
    return Profiler(
        resource=to_proto_resource({"service.name": "test"}),
        endpoint=endpoint,
        headers={"authorization": "Basic token"},
        interval=0.01,
        export_interval=10,
    )


def test_dictionary_reserves_zero_entries() -> None:
    """Every table starts with the empty entry required by the OTLP profiles format."""
    proto = _Dictionary().to_proto()

    assert list(proto.string_table) == [""]
    assert len(proto.function_table) == 1
    assert len(proto.location_table) == 1
    assert len(proto.stack_table) == 1


def test_dictionary_deduplicates_entries() -> None:
    """Repeated frames and stacks reuse the same table indices."""
    dictionary = _Dictionary()
    frame = ("app.py", "handler", 10)

    first = dictionary.stack((frame, ("app.py", "main", 3)))
    second = dictionary.stack((frame, ("app.py", "main", 3)))
    other = dictionary.stack((frame,))

    assert first == second
    assert other != first
    assert dictionary.location(frame) == 1
    assert len(dictionary.function_table) == 3


def test_frame_from_unwinder() -> None:
    """Frames read through the remote debugging API become (filename, function, line) tuples."""
    unwinder = _remote_debugging.RemoteUnwinder(os.getpid())
    frame = unwinder.get_stack_trace()[0].threads[0].frame_info[0]

    filename, funcname, lineno = _frame(frame)

    assert filename == __file__
    assert funcname == "test_frame_from_unwinder"
    assert lineno > 0


def test_to_proto_resource_keeps_value_types() -> None:
    """Resource attributes are converted to the matching OTLP value types."""
    resource = to_proto_resource({"name": "svc", "pid": 42, "ratio": 0.5, "debug": True, "tags": ("a", "b")})
    values = {attribute.key: attribute.value for attribute in resource.attributes}

    assert values["name"].string_value == "svc"
    assert values["pid"].int_value == 42
    assert values["ratio"].double_value == 0.5
    assert values["debug"].bool_value is True
    assert values["tags"].string_value == "('a', 'b')"


def test_build_request() -> None:
    """Samples are weighted by the sampling period and resolved through the shared dictionary."""
    profiler = _profiler()
    stack = (("app.py", "work", 5), ("app.py", "main", 1))

    request = profiler.build_request(Counter({stack: 3}), start=1_000, end=2_000)

    strings = request.dictionary.string_table
    profile = request.resource_profiles[0].scope_profiles[0].profiles[0]
    sample = profile.samples[0]
    leaf = request.dictionary.location_table[
        request.dictionary.stack_table[sample.stack_index].location_indices[0]
    ]
    assert isinstance(request.resource_profiles[0].resource, Resource)
    assert strings[profile.sample_type.type_strindex] == "cpu"
    assert strings[profile.sample_type.unit_strindex] == "nanoseconds"
    assert profile.period == 10_000_000
    assert profile.duration_nano == 1_000
    assert list(sample.values) == [30_000_000]
    assert strings[request.dictionary.function_table[leaf.lines[0].function_index].name_strindex] == "work"


def test_export_sends_protobuf(collector: OTLPCollector) -> None:
    """Exports are POSTed as protobuf with the configured headers."""
    _profiler(collector.endpoint).export(Counter({(("app.py", "work", 5),): 1}), start=0, end=1)

    headers, _ = collector.requests[0]
    assert headers["Content-Type"] == "application/x-protobuf"
    assert headers["authorization"] == "Basic token"
    assert "work" in collector.function_names()


def test_export_skips_empty_window(collector: OTLPCollector) -> None:
    """Nothing is sent when no samples were collected."""
    _profiler(collector.endpoint).export(Counter(), start=0, end=1)

    assert collector.requests == []


def test_export_failure_is_logged(caplog) -> None:
    """Export errors are logged instead of raised."""
    _profiler().export(Counter({(("app.py", "work", 5),): 1}), start=0, end=1)

    assert "Profiler export failed" in caplog.text
