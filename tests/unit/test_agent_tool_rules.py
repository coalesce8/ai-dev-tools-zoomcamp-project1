import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from runwatch.agent.clients import ToolError
from runwatch.agent.tools import (
    TOOLS,
    ToolConfig,
    ToolRunner,
    add_environment,
    is_write,
    tool_definitions,
)

ALL = [
    "get_stats",
    "list_jobs",
    "get_job_logs",
    "k8s_get_pods",
    "k8s_get_events",
    "k8s_rollout_history",
    "query_loki",
    "k8s_rollout_restart",
    "k8s_scale",
    "k8s_rollout_undo",
    "requeue_stalled_jobs",
    "requeue_failed_jobs",
]
NO_KUBE = [
    "get_stats",
    "list_jobs",
    "get_job_logs",
    "query_loki",
    "requeue_stalled_jobs",
    "requeue_failed_jobs",
]
WRITES = {
    "k8s_rollout_restart",
    "k8s_scale",
    "k8s_rollout_undo",
    "requeue_stalled_jobs",
    "requeue_failed_jobs",
}
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
JOB_ID = "0b9f6d52-3c1c-4a46-9a53-8d2f6d0f7a11"


class Recorder:
    """A fake client: every method call is recorded, answers come from `data`."""

    def __init__(self, **data):
        self.calls = []
        self.data = data
        self.namespace = "runwatch-dev"

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def method(*args):
            self.calls.append((name, *args))
            answer = self.data.get(name)
            if isinstance(answer, Exception):
                raise answer
            if callable(answer):
                return answer(*args)
            return answer

        return method


def make_runner(api=None, loki=None, kube="default", **kw):
    api = api or Recorder()
    loki = loki or Recorder()
    kube = Recorder() if kube == "default" else kube
    runner = ToolRunner(ToolConfig(env="dev"), api, loki, kube, clock=lambda: NOW)
    runner.fakes = (api, loki, kube)
    return runner


def calls_made(runner):
    return [c for fake in runner.fakes if fake for c in fake.calls]


def example(spec, runner_now=NOW):
    """A valid argument set built from the tool's own schema."""
    args = {}
    schema = spec.definition()["input_schema"]["properties"]
    for key, prop in schema.items():
        if "enum" in prop:
            args[key] = prop["enum"][0]
        elif prop["type"] == "integer":
            args[key] = prop["minimum"]
        elif prop.get("format") == "uuid":
            args[key] = str(uuid.uuid4())
        elif prop.get("format") == "date-time":
            delta = timedelta(minutes=10) if key == "since" else timedelta(0)
            args[key] = (runner_now - delta).isoformat()
        else:
            args[key] = '{service_name="x"}'
    return args


# --- the list ---


def test_tool_list_is_pinned():
    assert [t["name"] for t in tool_definitions(True)] == ALL
    assert [t["name"] for t in tool_definitions()] == ALL
    assert [t["name"] for t in tool_definitions(kube_enabled=False)] == NO_KUBE


def test_definitions_follow_the_tool_format():
    definitions = tool_definitions()
    json.dumps(definitions)
    for d in definitions:
        assert set(d) == {"name", "description", "input_schema"}
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", d["name"])
        assert d["description"].strip()
        schema = d["input_schema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema) == {"type", "properties", "required", "additionalProperties"}
        assert sorted(schema["required"]) == sorted(schema["properties"])
        assert not {"namespace", "namespaces"} & set(schema["properties"])
        wording = d["description"].lower()
        assert ("changes nothing" in wording) or wording.startswith("write")
    for name in ("get_stats", "k8s_get_events", "requeue_stalled_jobs"):
        schema = next(d for d in definitions if d["name"] == name)["input_schema"]
        assert schema["properties"] == {}


def test_is_write():
    assert {n for n in ALL if is_write(n)} == WRITES
    for name in ("", "kubectl", "GET_STATS"):
        with pytest.raises(KeyError):
            is_write(name)


def test_description_matches_is_write():
    for spec in TOOLS:
        says_write = spec.description.startswith("WRITE")
        assert says_write == is_write(spec.name)


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.name)
def test_validator_accepts_what_the_schema_describes(spec):
    runner = make_runner()
    args = example(spec)
    assert runner._validate(spec, args) is not None
    # one value outside each enum or range is refused
    props = spec.definition()["input_schema"]["properties"]
    for key, prop in props.items():
        bad = []
        if "enum" in prop:
            bad.append("NOT-IN-ENUM")
        if prop["type"] == "integer":
            bad += [prop["minimum"] - 1, prop["maximum"] + 1]
        for value in bad:
            result = runner.run(spec.name, {**args, key: value})
            assert result.refused and not result.executed, (spec.name, key)
    assert calls_made(runner) == []


# --- refusals ---


@pytest.mark.parametrize(
    "name",
    ["kubectl", "bash", "cancel_job", "k8s_delete_pod", "", "GET_STATS", None, 3],
)
def test_unknown_tool_is_refused(name):
    runner = make_runner()
    result = runner.run(name, {})
    assert (result.ok, result.refused, result.executed) == (False, True, False)
    assert "unknown tool" in result.content
    assert calls_made(runner) == []


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.name)
@pytest.mark.parametrize("key", ["namespace", "namespaces", "Namespace", "NAMESPACES"])
def test_namespace_argument_is_refused_everywhere(spec, key):
    runner = make_runner()
    valid = example(spec)
    for args in ({key: "runwatch-prod"}, {**valid, key: "runwatch-prod"}):
        result = runner.run(spec.name, args)
        assert (result.ok, result.refused, result.executed) == (False, True, False)
        assert "namespace" in result.content
    assert calls_made(runner) == []


def test_scale_with_namespace_is_refused():
    runner = make_runner()
    result = runner.run(
        "k8s_scale",
        {"deployment": "worker", "replicas": 2, "namespace": "runwatch-prod"},
    )
    assert result.refused and "namespace rule" in result.content
    assert calls_made(runner) == []


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.name)
def test_extra_and_non_object_arguments_are_refused(spec):
    runner = make_runner()
    for args in ({**example(spec), "extra": 1}, None, [], "x", 3):
        result = runner.run(spec.name, args)
        assert result.refused and not result.executed
    assert calls_made(runner) == []


@pytest.mark.parametrize(
    "deployment",
    [
        "loadgen",
        "frontend",
        "postgres",
        "Worker",
        "worker ",
        "worker,api",
        "",
        None,
        1,
        ["worker"],
    ],
)
@pytest.mark.parametrize(
    "tool", ["k8s_rollout_restart", "k8s_rollout_undo", "k8s_rollout_history"]
)
def test_bad_deployments_are_refused(tool, deployment):
    runner = make_runner()
    assert runner.run(tool, {"deployment": deployment}).refused
    assert runner.run(tool, {}).refused
    assert calls_made(runner) == []


def test_deployment_sets():
    for tool in ("k8s_rollout_restart", "k8s_rollout_undo", "k8s_rollout_history"):
        runner = make_runner()
        for name in ("worker", "api"):
            assert not runner.run(tool, {"deployment": name}).refused
    runner = make_runner()
    assert runner.run("k8s_scale", {"deployment": "api", "replicas": 2}).refused
    assert not runner.run("k8s_scale", {"deployment": "worker", "replicas": 2}).refused
    runner = make_runner()
    for app in ("api", "worker", "loadgen", "frontend"):
        assert not runner.run("k8s_get_pods", {"app": app}).refused
    for app in ("postgres", "Worker", "", None):
        assert runner.run("k8s_get_pods", {"app": app}).refused


@pytest.mark.parametrize("replicas", [0, 4, -1, 2.0, "2", True, None])
def test_bad_replicas_are_refused(replicas):
    runner = make_runner()
    assert runner.run(
        "k8s_scale", {"deployment": "worker", "replicas": replicas}
    ).refused
    assert runner.run("k8s_scale", {"deployment": "worker"}).refused
    assert calls_made(runner) == []


@pytest.mark.parametrize("replicas", [1, 2, 3])
def test_good_replicas_are_accepted(replicas):
    runner = make_runner(kube=Recorder(scale={"spec": {"replicas": replicas}}))
    assert runner.run("k8s_scale", {"deployment": "worker", "replicas": replicas}).ok


@pytest.mark.parametrize("state", ["queued", "Running", "DONE", "", None, 1])
def test_bad_job_state_is_refused(state):
    runner = make_runner()
    assert runner.run("list_jobs", {"state": state}).refused
    assert runner.run("list_jobs", {}).refused


@pytest.mark.parametrize(
    "job_id",
    [
        "../../x",
        "1",
        f"{JOB_ID}/cancel",
        f"{JOB_ID}?x=1",
        f" {JOB_ID}",
        JOB_ID.replace("-", ""),
        f"{{{JOB_ID}}}",
        f"urn:uuid:{JOB_ID}",
        None,
        5,
    ],
)
def test_bad_job_id_is_refused(job_id):
    runner = make_runner()
    assert runner.run("get_job_logs", {"id": job_id}).refused
    assert calls_made(runner) == []


def test_job_id_accepts_upper_and_lower_case():
    page = {"items": [], "next_after": 0, "has_more": False}
    runner = make_runner(api=Recorder(job_logs=page))
    assert runner.run("get_job_logs", {"id": JOB_ID}).ok
    assert runner.run("get_job_logs", {"id": JOB_ID.upper()}).ok


def window(since, until, runner=None):
    runner = runner or make_runner(api=Recorder(requeue_failed={"requeued": 2}))
    return runner, runner.run("requeue_failed_jobs", {"since": since, "until": until})


def test_requeue_failed_window_rules():
    ok = [
        ("2026-03-01T11:00:00Z", "2026-03-01T11:30:00Z"),
        ("2026-03-01T11:30:00Z", "2026-03-01T11:30:00Z"),  # since == until
        ("2026-03-01T10:00:00+00:00", "2026-03-01T12:00:00+00:00"),  # exactly 2 h
        ("2026-03-01T11:00:00Z", "2026-03-01T12:01:00Z"),  # exactly 60 s ahead
        ("2026-03-01T13:00:00+02:00", "2026-03-01T13:30:00+02:00"),
    ]
    for since, until in ok:
        runner, result = window(since, until)
        assert result.ok, (since, until, result.content)
        assert runner.fakes[0].calls[0][0] == "requeue_failed"
    bad = [
        ("2026-03-01T11:00:00", "2026-03-01T11:30:00"),  # no offset
        ("yesterday", "2026-03-01T11:30:00Z"),
        ("2026-03-01T11:00:00Z", "soon"),
        (None, "2026-03-01T11:30:00Z"),
        ("2026-03-01T11:00:00Z", 5),
        ("2026-03-01T11:30:00Z", "2026-03-01T11:00:00Z"),  # since after until
        ("2026-03-01T11:00:00Z", "2026-03-01T12:01:01Z"),  # future
        ("2026-03-01T09:59:59Z", "2026-03-01T12:00:00Z"),  # just over 2 h
        ("2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"),
    ]
    for since, until in bad:
        runner, result = window(since, until)
        assert result.refused and not result.executed, (since, until)
        assert runner.fakes[0].calls == []
    runner = make_runner()
    assert runner.run("requeue_failed_jobs", {"since": "2026-03-01T11:00:00Z"}).refused


def test_requeue_failed_sends_offset_strings():
    runner, result = window("2026-03-01T11:00:00Z", "2026-03-01T13:00:00+02:00")
    assert runner.fakes[0].calls == [
        ("requeue_failed", "2026-03-01T11:00:00+00:00", "2026-03-01T11:00:00+00:00")
    ]
    assert json.loads(result.content) == {"requeued": 2}


@pytest.mark.parametrize(
    "query",
    [
        'rate({a="b"}[1m])',
        'sum(count_over_time({a="b"}[1m]))',
        "",
        "{" + "a" * 500,
        '{a="b"',
        '{a="}"',
        None,
        5,
        ' {a="b"}',
    ],
)
def test_bad_loki_query_is_refused(query):
    runner = make_runner()
    assert runner.run("query_loki", {"query": query}).refused
    assert runner.run("query_loki", {}).refused
    assert calls_made(runner) == []


def test_loki_query_length_limit():
    runner = make_runner(loki=Recorder(query_range={"data": {"result": []}}))
    selector = '{a="' + "x" * 494 + '"}'
    assert len(selector) == 500
    assert runner.run("query_loki", {"query": selector}).ok
    assert runner.run("query_loki", {"query": selector + " "}).refused


def test_kube_tools_are_refused_when_disabled():
    runner = make_runner(kube=None)
    assert not runner.kube_enabled
    assert [d["name"] for d in runner.definitions()] == NO_KUBE
    for spec in TOOLS:
        if spec.kube:
            result = runner.run(spec.name, example(spec))
            assert (result.ok, result.refused, result.executed) == (False, True, False)
            assert "disabled" in result.content
    assert calls_made(runner) == []
    api = Recorder(requeue_stalled={"requeued": 1}, requeue_failed={"requeued": 0})
    runner = make_runner(api=api, kube=None)
    assert runner.run("requeue_stalled_jobs", {}).ok
    assert runner.run("requeue_failed_jobs", example(_spec("requeue_failed_jobs"))).ok


def _spec(name):
    return next(t for t in TOOLS if t.name == name)


# --- behaviour against fakes ---


def test_get_stats_returns_body_as_is():
    stats = {"counts": {"QUEUED": 1}, "as_of": "x"}
    runner = make_runner(api=Recorder(get_stats=stats))
    result = runner.run("get_stats", {})
    assert result.ok and result.executed and not result.refused
    assert json.loads(result.content) == stats


def test_list_jobs_drops_names_and_specs():
    item = {
        "id": JOB_ID,
        "name": "SECRET-NAME",
        "spec": {"duration_s": 1},
        "state": "STALLED",
        "attempt_count": 1,
        "max_attempts": 3,
        "created_at": "c",
        "started_at": "s",
        "finished_at": None,
        "last_error": "e" * 500,
    }
    api = Recorder(list_jobs={"items": [item], "next_cursor": "abc"})
    runner = make_runner(api=api)
    result = runner.run("list_jobs", {"state": "STALLED"})
    assert api.calls == [("list_jobs", "STALLED", 20)]
    assert "SECRET-NAME" not in result.content and "spec" not in result.content
    body = json.loads(result.content)
    assert body["has_more"] is True
    assert set(body["jobs"][0]) == {
        "id", "state", "attempt_count", "max_attempts", "created_at",
        "started_at", "finished_at", "last_error",
    }  # fmt: skip
    assert len(body["jobs"][0]["last_error"]) == 200
    api.data["list_jobs"] = {"items": [], "next_cursor": None}
    assert json.loads(runner.run("list_jobs", {"state": "FAILED"}).content) == {
        "jobs": [],
        "has_more": False,
    }


def log_page(first, last, more, pad=10):
    return {
        "items": [
            {"seq": n, "attempt_number": 1, "ts": "t", "line": f"line {n} " + "z" * pad}
            for n in range(first, last + 1)
        ],
        "next_after": last,
        "has_more": more,
    }


def test_job_logs_keeps_last_50_lines_and_follows_pages():
    pages = {0: log_page(1, 30, True), 30: log_page(31, 60, False)}
    api = Recorder(job_logs=lambda job_id, after: pages[after])
    result = make_runner(api=api).run("get_job_logs", {"id": JOB_ID})
    assert api.calls == [("job_logs", JOB_ID, 0), ("job_logs", JOB_ID, 30)]
    body = json.loads(result.content)
    assert [line["seq"] for line in body["lines"]] == list(range(11, 61))
    assert body["truncated"] is True  # lines were dropped
    assert set(body["lines"][0]) == {"seq", "attempt_number", "ts", "line"}


def test_job_log_lines_are_cut_to_200():
    api = Recorder(job_logs=log_page(1, 2, False, pad=300))
    body = json.loads(make_runner(api=api).run("get_job_logs", {"id": JOB_ID}).content)
    assert [len(line["line"]) for line in body["lines"]] == [200, 200]


def test_job_logs_few_lines_not_truncated():
    api = Recorder(job_logs=log_page(1, 3, False))
    body = json.loads(make_runner(api=api).run("get_job_logs", {"id": JOB_ID}).content)
    assert body["truncated"] is False and len(body["lines"]) == 3


def test_job_logs_stops_after_five_pages():
    api = Recorder(job_logs=lambda job_id, after: log_page(after + 1, after + 1, True))
    body = json.loads(make_runner(api=api).run("get_job_logs", {"id": JOB_ID}).content)
    assert len(api.calls) == 5
    assert body["truncated"] is True


def test_job_logs_404_is_job_not_found():
    api = Recorder(job_logs=ToolError("API returned 404: x", 404))
    result = make_runner(api=api).run("get_job_logs", {"id": JOB_ID})
    assert (result.ok, result.refused, result.executed) == (False, False, True)
    assert "job not found" in result.content


def test_requeue_stalled():
    api = Recorder(requeue_stalled={"requeued": 3, "x": 1})
    result = make_runner(api=api).run("requeue_stalled_jobs", {})
    assert api.calls == [("requeue_stalled",)]
    assert json.loads(result.content) == {"requeued": 3}


def test_loki_environment_is_inserted_after_the_selector():
    assert (
        add_environment('{service_name="runwatch-worker"}', "dev")
        == '{service_name="runwatch-worker"} | deployment_environment="dev"'
    )
    assert (
        add_environment('{service_name="a}b"} |= "x"', "dev")
        == '{service_name="a}b"} | deployment_environment="dev" |= "x"'
    )
    assert (
        add_environment('{a="q\\"}"} |= "}"', "dev")
        == '{a="q\\"}"} | deployment_environment="dev" |= "}"'
    )
    assert add_environment("{a=`}`}", "dev") == '{a=`}`} | deployment_environment="dev"'


def test_query_loki_call_and_result():
    values = [["1772366399000000000", "older"], ["1772366400000000000", "newer" * 100]]
    loki = Recorder(query_range={"data": {"result": [{"values": values}]}})
    runner = make_runner(loki=loki)
    result = runner.run(
        "query_loki", {"query": '{a="b"} | deployment_environment="prod"'}
    )
    end = int(NOW.timestamp()) * 10**9
    assert loki.calls == [
        (
            "query_range",
            '{a="b"} | deployment_environment="dev" | deployment_environment="prod"',
            end - 15 * 60 * 10**9,
            end,
            100,
        )
    ]
    entries = json.loads(result.content)["entries"]
    assert [e["line"][:5] for e in entries] == ["newer", "older"]
    assert len(entries[0]["line"]) == 300
    assert set(entries[0]) == {"ts", "line"}


def test_query_loki_caps_entries_at_100():
    values = [[str(10**18 + n), "x"] for n in range(150)]
    loki = Recorder(query_range={"data": {"result": [{"values": values}]}})
    result = make_runner(loki=loki).run("query_loki", {"query": '{a="b"}'})
    assert len(json.loads(result.content)["entries"]) == 100


# kubernetes


def deployment(selector=None, revision="3"):
    return {
        "metadata": {"annotations": {"deployment.kubernetes.io/revision": revision}},
        "spec": {"selector": {"matchLabels": selector or {"app": "worker", "t": "1"}}},
    }


def replicaset(owner, revision, image="img/worker:v1", created="2026-03-01T10:00:00Z",
               cause=None, hash_="abc"):  # fmt: skip
    annotations = {"deployment.kubernetes.io/revision": str(revision)}
    if cause:
        annotations["kubernetes.io/change-cause"] = cause
    return {
        "metadata": {
            "name": f"{owner}-{revision}",
            "creationTimestamp": created,
            "annotations": annotations,
            "ownerReferences": [{"kind": "Deployment", "name": owner}],
        },
        "spec": {
            "replicas": 1 if revision == 3 else 0,
            "template": {
                "metadata": {"labels": {"app": owner, "pod-template-hash": hash_}},
                "spec": {"containers": [{"name": "c", "image": image}]},
            },
        },
    }


def test_get_pods_uses_the_deployment_selector():
    pod = {
        "metadata": {"name": "worker-1"},
        "spec": {"containers": [{"image": "reg:5000/runwatch/worker:abc123"}]},
        "status": {
            "phase": "Running",
            "startTime": "2026-03-01T10:00:00Z",
            "containerStatuses": [
                {"ready": True, "restartCount": 2},
                {"ready": False, "restartCount": 1},
            ],
        },
    }
    kube = Recorder(get_deployment=deployment(), list_pods={"items": [pod]})
    result = make_runner(kube=kube).run("k8s_get_pods", {"app": "worker"})
    assert kube.calls == [("get_deployment", "worker"), ("list_pods", "app=worker,t=1")]
    assert json.loads(result.content) == {
        "pods": [
            {
                "name": "worker-1",
                "phase": "Running",
                "ready": "1/2",
                "restarts": 3,
                "started_at": "2026-03-01T10:00:00Z",
                "image_tags": ["abc123"],
            }
        ]
    }


def test_get_pods_zero_pods_and_missing_deployment():
    kube = Recorder(get_deployment=deployment(), list_pods={"items": []})
    result = make_runner(kube=kube).run("k8s_get_pods", {"app": "worker"})
    assert result.ok and json.loads(result.content) == {"pods": []}
    kube = Recorder(get_deployment=ToolError("Kubernetes returned 404: x", 404))
    result = make_runner(kube=kube).run("k8s_get_pods", {"app": "worker"})
    assert (
        not result.ok and result.executed and "deployment not found" in result.content
    )


def test_get_events_newest_50():
    events = [
        {
            "lastTimestamp": f"2026-03-01T10:{n:02d}:00Z",
            "type": "Warning",
            "reason": "BackOff",
            "involvedObject": {"kind": "Pod", "name": f"p{n}"},
            "message": "m" * 10,
            "count": n,
        }
        for n in range(60)
    ]
    kube = Recorder(list_events={"items": events})
    result = make_runner(kube=kube).run("k8s_get_events", {})
    body = json.loads(result.content)["events"]
    assert len(body) == 50
    assert body[0]["object"] == "Pod/p59" and body[0]["count"] == 59
    assert len(body[0]["message"]) == 10
    assert set(body[0]) == {"time", "type", "reason", "object", "message", "count"}


def test_event_message_is_cut_to_300():
    event = {"lastTimestamp": "t", "message": "m" * 500}
    kube = Recorder(list_events={"items": [event]})
    result = make_runner(kube=kube).run("k8s_get_events", {})
    assert len(json.loads(result.content)["events"][0]["message"]) == 300


def test_rollout_history_owned_newest_first():
    sets = [
        replicaset("worker", 1, "r/worker:v1"),
        replicaset("worker", 3, "r/worker:v3", "2026-03-01T11:55:00Z", "bad release"),
        replicaset("api", 5, "r/api:v9"),
        replicaset("worker", 2, "r/worker:v2"),
    ]
    kube = Recorder(get_deployment=deployment(), list_replicasets={"items": sets})
    result = make_runner(kube=kube).run("k8s_rollout_history", {"deployment": "worker"})
    revisions = json.loads(result.content)["revisions"]
    assert [r["revision"] for r in revisions] == [3, 2, 1]
    assert revisions[0] == {
        "revision": 3,
        "created_at": "2026-03-01T11:55:00Z",
        "image_tag": "v3",
        "change_cause": "bad release",
        "replicas": 1,
    }
    assert revisions[1]["change_cause"] is None


def test_rollout_history_at_most_10():
    sets = [replicaset("worker", n) for n in range(1, 15)]
    kube = Recorder(get_deployment=deployment(), list_replicasets={"items": sets})
    result = make_runner(kube=kube).run("k8s_rollout_history", {"deployment": "worker"})
    assert len(json.loads(result.content)["revisions"]) == 10


def test_scale_and_restart_calls():
    kube = Recorder(scale={"spec": {"replicas": 2}}, merge_patch_deployment={})
    runner = make_runner(kube=kube)
    result = runner.run("k8s_scale", {"deployment": "worker", "replicas": 2})
    assert kube.calls == [("scale", "worker", 2)]
    assert json.loads(result.content) == {"deployment": "worker", "replicas": 2}
    result = runner.run("k8s_rollout_restart", {"deployment": "api"})
    stamp = NOW.isoformat()
    assert kube.calls[1] == (
        "merge_patch_deployment",
        "api",
        {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"kubectl.kubernetes.io/restartedAt": stamp}
                    }
                }
            }
        },
    )
    assert json.loads(result.content) == {"deployment": "api", "restarted_at": stamp}


def test_rollout_undo_goes_back_one_revision():
    sets = [
        replicaset("worker", 1, "r/worker:v1", hash_="h1"),
        replicaset("worker", 2, "r/worker:v2", hash_="h2"),
        replicaset("worker", 3, "r/worker:v3", hash_="h3"),
        replicaset("api", 2, "r/api:v2", hash_="h9"),
    ]
    kube = Recorder(
        get_deployment=deployment(revision="3"),
        list_replicasets={"items": sets},
        json_patch_deployment={},
    )
    result = make_runner(kube=kube).run("k8s_rollout_undo", {"deployment": "worker"})
    name, deployment_name, ops = kube.calls[-1]
    assert (name, deployment_name) == ("json_patch_deployment", "worker")
    assert len(ops) == 1 and ops[0]["op"] == "replace"
    assert ops[0]["path"] == "/spec/template"
    template = ops[0]["value"]
    assert template["metadata"]["labels"] == {"app": "worker"}
    assert template["spec"]["containers"][0]["image"] == "r/worker:v2"
    assert json.loads(result.content) == {
        "deployment": "worker",
        "rolled_back_to_revision": 2,
    }
    # the ReplicaSet object itself is not changed
    assert (
        sets[1]["spec"]["template"]["metadata"]["labels"]["pod-template-hash"] == "h2"
    )


def test_rollout_undo_without_previous_revision_sends_no_patch():
    kube = Recorder(
        get_deployment=deployment(revision="1"),
        list_replicasets={"items": [replicaset("worker", 1)]},
    )
    result = make_runner(kube=kube).run("k8s_rollout_undo", {"deployment": "worker"})
    assert (result.ok, result.refused, result.executed) == (False, False, True)
    assert "no previous revision" in result.content
    assert not [c for c in kube.calls if c[0].endswith("patch_deployment")]


# --- failures and limits ---


@pytest.mark.parametrize(
    "error",
    [ToolError("API returned 503: busy", 503), ToolError("request to API timed out")],
)
def test_client_errors_are_results(error):
    api = Recorder(get_stats=error)
    result = make_runner(api=api).run("get_stats", {})
    assert (result.ok, result.refused, result.executed) == (False, False, True)
    assert error.reason in result.content
    assert len(api.calls) == 1  # never retried


def test_broken_response_is_a_result_not_an_exception():
    api = Recorder(requeue_stalled={"unexpected": 1})
    result = make_runner(api=api).run("requeue_stalled_jobs", {})
    assert (result.ok, result.refused, result.executed) == (False, False, True)


def test_large_results_are_cut():
    big = {"text": "x" * 100_000}
    result = make_runner(api=Recorder(get_stats=big)).run("get_stats", {})
    assert len(result.content) == 8000 and result.truncated
    assert result.content.endswith("...[truncated]")
    small = make_runner(api=Recorder(get_stats={"a": 1})).run("get_stats", {})
    assert not small.truncated


def test_tool_result_is_frozen():
    result = make_runner(api=Recorder(get_stats={})).run("get_stats", {})
    with pytest.raises(AttributeError):
        result.ok = False


# --- config ---


def test_config_from_environment():
    default = ToolConfig.from_environment({})
    assert default == ToolConfig(
        "http://localhost:8000", "http://localhost:3100", "local", None, 10
    )
    config = ToolConfig.from_environment(
        {
            "RUNWATCH_API_URL": "http://api:8000",
            "AGENT_LOKI_URL": "http://loki:3100",
            "AGENT_ENV": "prod",
            "AGENT_K8S_NAMESPACE": "runwatch-prod",
            "AGENT_TOOL_TIMEOUT_S": "2.5",
        }
    )
    assert config.env == "prod" and config.timeout_s == 2.5
    assert config.k8s_namespace == "runwatch-prod"
    for bad in ("0", "-1", "abc"):
        with pytest.raises(Exception, match="AGENT_TOOL_TIMEOUT_S"):
            ToolConfig.from_environment({"AGENT_TOOL_TIMEOUT_S": bad})


# --- logging and privacy ---

MARKERS = {
    "name": "MARK-JOBNAME-1",
    "log": "MARK-LOGLINE-2",
    "loki": "MARK-LOKILINE-3",
    "event": "MARK-EVENTMSG-4",
    "anthropic": "MARK-ANTHROPIC-5",
    "kube": "MARK-KUBETOKEN-6",
    "github": "MARK-GITHUB-7",
    "query": "MARK-QUERY-8",
}


def test_logs_carry_no_free_text_or_secrets(caplog, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", MARKERS["anthropic"])
    monkeypatch.setenv("GITHUB_TOKEN", MARKERS["github"])
    monkeypatch.setenv("KUBE_TOKEN", MARKERS["kube"])
    api = Recorder(
        get_stats={"note": MARKERS["name"]},
        list_jobs={"items": [{"name": MARKERS["name"], "last_error": MARKERS["log"]}]},
        job_logs=log_page(1, 1, False)
        | {"items": [{"seq": 1, "line": MARKERS["log"]}]},
        requeue_stalled={"requeued": 1},
        requeue_failed={"requeued": 1},
    )
    loki = Recorder(
        query_range={"data": {"result": [{"values": [["1", MARKERS["loki"]]]}]}}
    )
    kube = Recorder(
        get_deployment=deployment(),
        list_pods={"items": []},
        list_events={"items": [{"message": MARKERS["event"], "lastTimestamp": "t"}]},
        list_replicasets={"items": [replicaset("worker", 1), replicaset("worker", 2)]},
        scale={"spec": {"replicas": 1}},
        merge_patch_deployment={},
        json_patch_deployment={},
    )
    runner = make_runner(api, loki, kube)
    caplog.set_level(logging.DEBUG)
    results = []
    for spec in TOOLS:
        args = example(spec)
        if spec.name == "query_loki":
            args = {"query": '{a="' + MARKERS["query"] + '"}'}
        results.append(runner.run(spec.name, args))
    # refusals and failures with markers in the arguments too
    results.append(runner.run(MARKERS["query"], {}))
    results.append(runner.run("get_stats", {MARKERS["query"]: 1}))
    results.append(runner.run("list_jobs", {"state": MARKERS["query"]}))
    api.data["get_stats"] = ToolError("API returned 500: boom", 500)
    results.append(runner.run("get_stats", {}))
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("runwatch")]
    assert len([ln for ln in lines if ln.startswith("tool call")]) == len(results)
    everything = "\n".join(
        r.getMessage() + str(r.__dict__.get("args", "")) for r in caplog.records
    )
    for marker in MARKERS.values():
        assert marker not in everything
    assert any("query_length=" in ln for ln in lines)
    assert any("replicas=1" in ln and "deployment=worker" in ln for ln in lines)
