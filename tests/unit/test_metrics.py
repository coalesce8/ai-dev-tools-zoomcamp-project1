"""runwatch.metrics and telemetry.build_meter_provider, no database."""

import re
from pathlib import Path

import pytest
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    InMemoryMetricReader,
    PeriodicExportingMetricReader,
)
from runwatch import metrics as m
from runwatch import queue_stats, telemetry
from runwatch.lifecycle import AttemptState, FailureCause, JobState
from runwatch.telemetry import METRIC_EXPORT_INTERVAL_MS, build_meter_provider

SPEC_PATH = Path(__file__).resolve().parents[2] / "_docs" / "SPEC.md"


@pytest.fixture(autouse=True)
def _no_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)


def readers(provider):
    return tuple(provider._metric_readers)


def read(reader):
    """{(name, frozenset of attributes): data point} over everything exported."""
    data = reader.get_metrics_data()
    found = {}
    if data is None:
        return found
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for point in metric.data.data_points:
                    key = (metric.name, frozenset(point.attributes.items()))
                    found[key] = point
    return found


def value(found, name, **attrs):
    return found[(name, frozenset(attrs.items()))].value


class FakeSession:
    closed = 0

    def close(self):
        FakeSession.closed += 1


def build(fake_counts=None, now_age=None):
    reader = InMemoryMetricReader()
    provider = build_meter_provider("runwatch-api", reader)
    meter = provider.get_meter("t")
    metrics = m.build_metrics(meter)
    m.register_db_gauges(meter, FakeSession)
    return reader, provider, metrics


@pytest.fixture
def stub_queue(monkeypatch):
    state = {"counts": {s.value: 0 for s in JobState}, "age": None, "fail": False}

    def count_by_state(session):
        if state["fail"]:
            raise RuntimeError("postgresql://user:secret@host/db")
        return state["counts"]

    monkeypatch.setattr(queue_stats, "count_by_state", count_by_state)
    monkeypatch.setattr(queue_stats, "database_now", lambda s: "now")
    monkeypatch.setattr(
        queue_stats, "oldest_queued_age_seconds", lambda s, n: state["age"]
    )
    return state


def test_provider_without_endpoint_has_no_reader():
    provider = build_meter_provider("runwatch-api")
    assert provider._sdk_config.resource.attributes["service.name"] == "runwatch-api"
    assert readers(provider) == ()
    metrics = m.build_metrics(provider.get_meter("t"))
    metrics.submitted()
    provider.shutdown()


@pytest.mark.parametrize("value_", ["http://collector:4318", "http://collector:4318/"])
def test_endpoint_builds_periodic_reader(monkeypatch, value_):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", value_)
    provider = build_meter_provider("runwatch-api")
    (reader,) = readers(provider)
    assert isinstance(reader, PeriodicExportingMetricReader)
    assert isinstance(reader._exporter, OTLPMetricExporter)
    assert reader._exporter._endpoint == "http://collector:4318/v1/metrics"
    assert reader._export_interval_millis == 10_000
    assert METRIC_EXPORT_INTERVAL_MS == 10_000
    assert telemetry.METRIC_EXPORT_INTERVAL_MS == 10_000


def test_explicit_reader_wins_over_environment(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    reader = InMemoryMetricReader()
    provider = build_meter_provider("runwatch-api", reader)
    assert readers(provider) == (reader,)
    assert isinstance(provider, MeterProvider)


def test_instrument_names_match_the_spec(stub_queue):
    table = SPEC_PATH.read_text()
    section = table.split("Metrics:\n", 1)[1].split("All job metrics", 1)[0]
    spec_names = set(re.findall(r"^\| `(runwatch_\w+)`", section, re.M))
    assert len(spec_names) == 8
    reader, _, metrics = build()
    metrics.claimed(1)
    metrics.result_reported(AttemptState.SUCCEEDED, None, JobState.SUCCEEDED, 1)
    found = {name for name, _ in read(reader)}
    assert found == spec_names


def test_instrument_types(stub_queue):
    reader, _, _ = build()
    data = reader.get_metrics_data()
    kinds = {
        metric.name: type(metric.data).__name__
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
    }
    for name in (m.JOBS_SUBMITTED, m.JOBS_FINISHED, m.ATTEMPTS_FINISHED):
        assert kinds[name] == "Sum"
    for name in (m.QUEUE_DEPTH, m.OLDEST_QUEUED_AGE, m.JOBS_STALLED):
        assert kinds[name] == "Gauge"


def test_counter_series_start_at_zero(stub_queue):
    reader, _, _ = build()
    found = read(reader)
    assert value(found, m.JOBS_SUBMITTED) == 0
    for outcome in ("succeeded", "failed", "cancelled"):
        assert value(found, m.JOBS_FINISHED, outcome=outcome) == 0
    for outcome, cause in (
        ("succeeded", "none"),
        ("failed", "workload"),
        ("failed", "infra"),
    ):
        assert value(found, m.ATTEMPTS_FINISHED, outcome=outcome, cause=cause) == 0
    assert len([k for k in found if k[0] == m.ATTEMPTS_FINISHED]) == 3
    assert len([k for k in found if k[0] == m.JOBS_FINISHED]) == 3


def test_label_values_are_not_hand_written():
    source = (Path(m.__file__)).read_text()
    pattern = re.compile(r'"(succeeded|failed|cancelled|workload|infra)"')
    assert pattern.findall(source) == []


def test_event_methods(stub_queue):
    reader, _, metrics = build()
    metrics.submitted()
    metrics.claimed(90)
    metrics.claimed(-5)
    metrics.result_reported(AttemptState.SUCCEEDED, None, JobState.SUCCEEDED, 30)
    metrics.result_reported(
        AttemptState.FAILED, FailureCause.WORKLOAD, JobState.QUEUED, 4
    )
    metrics.result_reported(AttemptState.FAILED, FailureCause.INFRA, JobState.FAILED, 2)
    metrics.swept(3)
    metrics.swept(0)
    metrics.cancelled()
    found = read(reader)
    assert value(found, m.JOBS_SUBMITTED) == 1
    assert value(found, m.JOBS_FINISHED, outcome="succeeded") == 1
    assert value(found, m.JOBS_FINISHED, outcome="failed") == 1
    assert value(found, m.JOBS_FINISHED, outcome="cancelled") == 1
    assert value(found, m.ATTEMPTS_FINISHED, outcome="succeeded", cause="none") == 1
    assert value(found, m.ATTEMPTS_FINISHED, outcome="failed", cause="workload") == 1
    assert value(found, m.ATTEMPTS_FINISHED, outcome="failed", cause="infra") == 4
    wait = found[(m.QUEUE_WAIT, frozenset())]
    assert wait.count == 2 and wait.sum == 90
    run = found[(m.JOB_RUN, frozenset({("outcome", "succeeded")}))]
    assert run.count == 1 and run.sum == 30
    assert found[(m.JOB_RUN, frozenset({("outcome", "failed")}))].count == 2


def test_histogram_buckets(stub_queue):
    reader, _, metrics = build()
    metrics.claimed(1)
    metrics.result_reported(AttemptState.SUCCEEDED, None, JobState.SUCCEEDED, 1)
    found = read(reader)
    assert tuple(found[(m.QUEUE_WAIT, frozenset())].explicit_bounds) == (
        0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600,
    )  # fmt: skip
    run = found[(m.JOB_RUN, frozenset({("outcome", "succeeded")}))]
    assert tuple(run.explicit_bounds) == (1, 2, 5, 10, 20, 30, 60, 120, 300)


def test_empty_queue_gauges_are_zero(stub_queue):
    reader, _, _ = build()
    found = read(reader)
    assert value(found, m.QUEUE_DEPTH) == 0
    assert value(found, m.JOBS_STALLED) == 0
    assert value(found, m.OLDEST_QUEUED_AGE) == 0


def test_gauges_read_at_each_collection(stub_queue):
    reader, _, _ = build()
    stub_queue["counts"][JobState.QUEUED.value] = 3
    stub_queue["counts"][JobState.STALLED.value] = 2
    stub_queue["counts"][JobState.RUNNING.value] = 9
    stub_queue["age"] = 200.0
    found = read(reader)
    assert value(found, m.QUEUE_DEPTH) == 3
    assert value(found, m.JOBS_STALLED) == 2
    assert value(found, m.OLDEST_QUEUED_AGE) == 200.0
    stub_queue["counts"][JobState.QUEUED.value] = 4
    assert value(read(reader), m.QUEUE_DEPTH) == 4


def test_database_error_leaves_gauges_without_data(stub_queue, caplog):
    reader, _, metrics = build()
    metrics.submitted()
    stub_queue["fail"] = True
    FakeSession.closed = 0
    found = read(reader)
    names = {name for name, _ in found}
    assert m.QUEUE_DEPTH not in names and m.JOBS_STALLED not in names
    assert value(found, m.JOBS_SUBMITTED) == 1
    assert FakeSession.closed >= 2
    assert "secret" not in caplog.text and "RuntimeError" in caplog.text


def test_no_reader_never_queries(monkeypatch):
    def boom(session):
        raise AssertionError("queried")

    monkeypatch.setattr(queue_stats, "count_by_state", boom)
    provider = build_meter_provider("runwatch-api")
    m.register_db_gauges(provider.get_meter("t"), FakeSession)
    provider.shutdown()


def test_metrics_module_has_no_sql():
    source = Path(m.__file__).read_text()
    assert re.findall(r"select\(|text\(|\.execute\(", source) == []
