"""Tracing and metrics setup for the Helix agent.

Tracer and meter providers default to console-free, no-op-ish exporters so
nothing external is required to run. A later subphase can swap in a real
OTLP exporter without touching call sites.

Counters and histograms are defined here even though nothing calls them
yet, since there are no agents in this subphase - they exist so 1.5+ has
somewhere to record LLM call and evidence collection metrics from day one.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider

trace.set_tracer_provider(TracerProvider())
metrics.set_meter_provider(MeterProvider())

tracer = trace.get_tracer("helix")
meter = metrics.get_meter("helix")

llm_call_counter = meter.create_counter(
    name="helix.llm.calls",
    description="Number of LLM calls made by Helix agents",
)
llm_call_latency = meter.create_histogram(
    name="helix.llm.call.duration",
    description="Duration of LLM calls in seconds",
    unit="s",
)
evidence_collection_counter = meter.create_counter(
    name="helix.evidence.collections",
    description="Number of evidence collection operations",
)


@contextmanager
def trace_workflow_step(name: str) -> Iterator[None]:
    """Trace a single workflow step as a span named `name`."""
    with tracer.start_as_current_span(name):
        yield
