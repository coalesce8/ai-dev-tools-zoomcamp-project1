import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from runwatch.agent.incidents import IncidentStore
from runwatch.agent.reports import REVERT_NOTE, redact, render_report, write_report

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 3, 1, 13, tzinfo=UTC)
SECTIONS = [
    "## Summary",
    "## Alert",
    "## Timeline",
    "## Evidence",
    "## Actions taken",
    "## Outcome",
    "## What a human should check",
    "## Repeated webhooks",
]


def copy_fixture(tmp_path, name="incident_bad_release"):
    target = tmp_path / "20260301T120000Z-bad1"
    shutil.copytree(FIXTURES / name, target)
    return target


def write_actions(directory, *rows):
    (directory / "actions.jsonl").write_text(
        "".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows)
    )


def entry(tool, ok=True, write=False, summary="done", args=None, **extra):
    return {
        "ts": "2026-03-01T12:01:00Z",
        "kind": "tool",
        "tool": tool,
        "args": args or {},
        "write": write,
        "ok": ok,
        "summary": summary,
        **extra,
    }


def set_status(directory, **changes):
    path = directory / "status.json"
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize("name", ["incident_bad_release", "incident_worker_down"])
def test_golden_reports(name):
    expected = (FIXTURES / name / "expected_report.md").read_text()
    assert render_report(FIXTURES / name, NOW) == expected
    assert "nightly-report" not in expected
    assert ("revert PR" in expected) == (name == "incident_bad_release")


def test_sections_in_order(tmp_path):
    text = render_report(copy_fixture(tmp_path), NOW)
    assert text.startswith("# Incident 20260301T120000Z-bad1\n")
    positions = [text.index(s + "\n") for s in SECTIONS]
    assert positions == sorted(positions)


def test_rendering_is_deterministic(tmp_path):
    directory = copy_fixture(tmp_path)
    assert render_report(directory, NOW) == render_report(directory, NOW)


def test_timeline_is_chronological(tmp_path):
    text = render_report(copy_fixture(tmp_path), NOW)
    timeline = text.split("## Timeline")[1].split("## Evidence")[0]
    stamps = [
        line.split()[1] for line in timeline.splitlines() if line.startswith("- ")
    ]
    assert stamps == sorted(stamps) and len(stamps) == 15


def test_refused_is_not_failed_and_never_reverts(tmp_path):
    directory = copy_fixture(tmp_path)
    write_actions(
        directory,
        entry(
            "k8s_rollout_undo",
            ok=False,
            write=True,
            summary="refused by tool rules",
            args={"deployment": "api"},
        ),
        entry("k8s_scale", ok=False, write=True, summary="kube api timeout"),
        entry("get_stats", ok=False, summary="refused by tool rules"),
        entry("query_loki", ok=False, summary="timeout"),
    )
    text = render_report(directory, NOW)
    assert "`k8s_rollout_undo` (deployment=api): refused" in text
    assert "`k8s_scale`: failed" in text
    assert "`get_stats`: refused" in text and "`query_loki`: failed" in text
    assert REVERT_NOTE not in text


def test_failed_undo_and_other_writes_have_no_revert_note(tmp_path):
    directory = copy_fixture(tmp_path)
    write_actions(
        directory,
        entry("k8s_rollout_undo", ok=False, write=True, summary="no previous revision"),
        entry("k8s_scale", write=True),
        entry("requeue_failed_jobs", write=True),
    )
    assert REVERT_NOTE not in render_report(directory, NOW)


def test_revert_note_in_actions_and_human_check(tmp_path):
    text = render_report(copy_fixture(tmp_path), NOW)
    actions = text.split("## Actions taken")[1].split("## Outcome")[0]
    check = text.split("## What a human should check")[1].split("## Repeated")[0]
    assert REVERT_NOTE in actions and REVERT_NOTE in check


def test_empty_sections_and_missing_files(tmp_path):
    directory = copy_fixture(tmp_path)
    for name in ("actions.jsonl", "summary.md"):
        (directory / name).unlink()
    text = render_report(directory, NOW)
    assert text.count("None recorded.") == 2
    assert "Agent summary" not in text and "unreadable" not in text


def test_skipped_no_model(tmp_path):
    directory = copy_fixture(tmp_path)
    (directory / "actions.jsonl").unlink()
    set_status(directory, outcome="skipped_no_model")
    text = render_report(directory, NOW)
    assert "ANTHROPIC_API_KEY" in text.split("## What a human should check")[1]
    assert text.count("None recorded.") == 2
    for section in SECTIONS:
        assert section in text


def test_escalated_and_interrupted_texts(tmp_path):
    directory = copy_fixture(tmp_path)
    set_status(directory, outcome="escalated")
    text = render_report(directory, NOW)
    assert "may still be true" in text and "interrupted" not in text
    set_status(directory, interrupted=True)
    assert "interrupted by an agent restart" in render_report(directory, NOW)


def test_running_is_partial(tmp_path):
    directory = copy_fixture(tmp_path)
    set_status(directory, state="running", outcome=None, ended_at=None)
    text = render_report(directory, NOW)
    assert "Partial report" in text and "In progress" in text
    assert "duration: 1h 0m" not in text and "so far" in text


def test_last_verification_wins_and_still_true(tmp_path):
    directory = copy_fixture(tmp_path)
    rows = [
        {**entry("get_stats"), "kind": "verification", "ts": "2026-03-01T12:02:00Z"},
        {
            **entry("get_stats", ok=False),
            "kind": "verification",
            "ts": "2026-03-01T12:03:00Z",
        },
    ]
    write_actions(directory, *rows)
    text = render_report(directory, NOW)
    assert "last verification: condition still true at 2026-03-01T12:03:00Z" in text


def test_agent_summary_cut_at_2000(tmp_path):
    directory = copy_fixture(tmp_path)
    (directory / "summary.md").write_text("a" * 5000 + "TAIL")
    text = render_report(directory, NOW)
    assert "model-written, unverified" in text
    assert "a" * 2000 in text and "a" * 2001 not in text and "TAIL" not in text


def test_unreadable_entries_are_counted_not_raised(tmp_path):
    directory = copy_fixture(tmp_path)
    write_actions(
        directory,
        "not json",
        json.dumps({"tool": "get_stats"}),
        json.dumps({"ts": "2026-03-01T12:00:00Z"}),
        json.dumps([1]),
        entry("get_stats"),
    )
    text = render_report(directory, NOW)
    assert "4 unreadable entries" in text and "`get_stats`: ok" in text


def test_bad_status_raises_value_error(tmp_path):
    directory = copy_fixture(tmp_path)
    (directory / "status.json").write_text("{")
    with pytest.raises(ValueError):
        render_report(directory, NOW)


def test_privacy_markers_never_reach_the_report(tmp_path):
    directory = copy_fixture(tmp_path)
    (directory / "context.md").write_text(
        "job nightly-secret-job\nlog LOGLINE-MARKER\n"
    )
    write_actions(
        directory,
        entry(
            "query_loki",
            args={"query_chars": 42, "query": '{app="x"} |= "QUERY-MARKER"'},
            extra_field="JOBNAME-MARKER LOGLINE-MARKER",
            result="RESULT-MARKER",
        ),
        entry(
            "get_job",
            args={"id": "7", "name": "NAME-MARKER", "id2": "x"},
            extra="nightly-secret-job",
        ),
        entry("k8s_scale", write=True, args={"deployment": "worker bad name\n## x"}),
    )
    text = render_report(directory, NOW)
    for marker in (
        "nightly-secret-job",
        "LOGLINE-MARKER",
        "QUERY-MARKER",
        "JOBNAME-MARKER",
        "RESULT-MARKER",
        "NAME-MARKER",
        "bad name",
    ):
        assert marker not in text
    assert "`query_loki` (query, 42 chars): ok" in text
    assert "`get_job` (id=7): ok" in text and "deployment=(omitted)" in text


def test_redaction_of_values_and_patterns(tmp_path):
    directory = copy_fixture(tmp_path)
    token = "tok-" + "s3cretvalue"
    gh = "ghp_" + "A" * 24
    write_actions(
        directory,
        entry(
            "get_stats",
            summary=f"{token} sk-ant-abc_123 {gh} github_pat_X1_y "
            "postgresql://user:pw@host/db",
        ),
    )
    text = redact(render_report(directory, NOW), [token, ""])
    for leaked in (token, "sk-ant-abc", gh, "github_pat_X1", "user:pw", "pw@host"):
        assert leaked not in text
    assert "[redacted]" in text


def test_write_report_redacts_and_writes_atomically(tmp_path):
    copy_fixture(tmp_path)
    store = IncidentStore(tmp_path)
    iid = "20260301T120000Z-bad1"
    write_actions(tmp_path / iid, entry("get_stats", summary="key KEYVALUE1234"))
    write_report(store, iid, NOW, ["KEYVALUE1234"])
    text = (tmp_path / iid / "response.md").read_text()
    assert "KEYVALUE1234" not in text and "[redacted]" in text
    assert [p.name for p in (tmp_path / iid).glob(".*tmp")] == []


def test_write_report_falls_back_when_rendering_fails(tmp_path):
    copy_fixture(tmp_path)
    store = IncidentStore(tmp_path)
    iid = "20260301T120000Z-bad1"
    (tmp_path / iid / "status.json").write_text("{")
    write_report(store, iid, NOW)
    assert "report generation failed" in (tmp_path / iid / "response.md").read_text()
