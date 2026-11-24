import threading
from collections.abc import Generator
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import override

import pytest
from opentelemetry.proto.collector.profiles.v1development.profiles_service_pb2 import (
    ExportProfilesServiceRequest,
)

from otel_profiling import ProfilingInstrumentor


class OTLPCollector:
    """In-memory OTLP profiles receiver backed by a real HTTP server."""

    def __init__(self) -> None:
        self.requests: list[tuple[Message, ExportProfilesServiceRequest]] = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                collector.requests.append(
                    (self.headers, ExportProfilesServiceRequest.FromString(body)),
                )
                self.send_response(200)
                self.end_headers()

            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1/profiles"

    def function_names(self) -> set[str]:
        """Names of every function referenced by the received profiles."""
        names: set[str] = set()
        for _, request in self.requests:
            strings = request.dictionary.string_table
            names.update(strings[function.name_strindex] for function in request.dictionary.function_table)

        return names


@pytest.fixture
def collector() -> Generator[OTLPCollector]:
    """Local OTLP profiles receiver."""
    otlp = OTLPCollector()
    thread = threading.Thread(target=otlp.server.serve_forever, daemon=True)
    thread.start()
    yield otlp
    otlp.server.shutdown()


@pytest.fixture(params=["process", "thread"])
def instrumentor(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Generator[ProfilingInstrumentor]:
    """Profiling instrumentor with the OTLP exporter enabled in each mode, uninstrumented after the test."""
    monkeypatch.setenv("OTEL_PROFILES_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_PYTHON_PROFILING_MODE", request.param)
    instance = ProfilingInstrumentor()
    yield instance
    instance.uninstrument()
