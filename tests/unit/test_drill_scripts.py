"""Offline tests for scripts/drills with a stub kubectl; no cluster is contacted."""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DRILLS = ROOT / "scripts" / "drills"
SCRIPTS = ["rollback", "break-worker", "bad-release", "heal"]
ALL = ["api", "worker", "frontend", "loadgen", "agent", "otel-collector"]
BASE = "--kubeconfig {kc} --context kind-runwatch -n runwatch-{env}"

STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
case "$*" in
  *jsonpath*WORKER_FAULT*) echo -n "${STUB_FAULT:-}"; exit 0 ;;
  *".spec.replicas"*) echo -n "${STUB_REPLICAS:-1}"; exit 0 ;;
esac
if [ -n "${STUB_FAIL:-}" ] && [[ "$*" == *"${STUB_FAIL%%=*}"* ]]; then
  echo "${STUB_FAIL#*=}" >&2
  exit 1
fi
exit 0
"""


@pytest.fixture
def sandbox(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "kubectl"
    stub.write_text(STUB)
    stub.chmod(0o755)
    kc = tmp_path / "kubeconfig"
    kc.write_text("x")
    log = tmp_path / "log"
    log.write_text("")

    def run(script, *, env=None, kubeconfig=None, stub_path=True):
        e = {
            "PATH": f"{bindir}:{os.environ['PATH']}"
            if stub_path
            else os.environ["PATH"],
            "HOME": str(tmp_path),
            "KIND_KUBECONFIG": str(kubeconfig or kc),
            "STUB_LOG": str(log),
        }
        e.update(env or {})
        r = subprocess.run(
            [str(DRILLS / f"{script}.sh")],
            env=e,
            capture_output=True,
            text=True,
            timeout=30,
        )
        calls = log.read_text().splitlines()
        return r, calls

    run.kc = kc
    return run


def prefix(sandbox, env="dev"):
    return BASE.format(kc=sandbox.kc, env=env)


def test_scripts_are_well_formed():
    for name in SCRIPTS:
        path = DRILLS / f"{name}.sh"
        assert path.stat().st_mode & stat.S_IXUSR
        lines = path.read_text().splitlines()
        assert lines[0] == "#!/usr/bin/env bash"
        assert "set -euo pipefail" in lines
        subprocess.run(["bash", "-n", str(path)], check=True)
    subprocess.run(["bash", "-n", str(DRILLS / "common.sh")], check=True)
    assert not (DRILLS / "common.sh").stat().st_mode & stat.S_IXUSR


def test_no_destructive_words_and_no_bare_kubectl():
    pattern = re.compile(
        r"\bdelete\b|\brm\b|--rm|--force|replace|kind (delete|create)"
        r"|docker (rm|stop|kill)"
    )
    for path in DRILLS.glob("*.sh"):
        text = path.read_text()
        assert not pattern.search(text), path
        assert "agent-relay" not in text
        if path.name != "common.sh":
            assert "kubectl" not in re.sub(r"#.*", "", text).replace("DRY_RUN", ""), (
                path
            )
    makefile = (ROOT / "Makefile").read_text()
    assert "agent-relay" not in makefile
    tail = makefile[makefile.index("\nrollback:") :]
    assert not pattern.search(tail)
    for target in ["rollback", "break-worker", "bad-release", "heal"]:
        assert re.search(rf"^\.PHONY:.*\b{target}\b", makefile, re.M)
    assert "KIND_KUBECONFIG ?= $(HOME)/.kube/runwatch-kind.config" in makefile


@pytest.mark.parametrize("env", [{}, {"ENV": "staging"}])
def test_rollback_needs_valid_env(sandbox, env):
    r, calls = sandbox("rollback", env=env)
    assert r.returncode == 2
    assert "usage" in r.stderr
    assert calls == []


def test_rollback_prod_needs_confirm(sandbox):
    r, calls = sandbox("rollback", env={"ENV": "prod"})
    assert r.returncode != 0 and "usage" in r.stderr and calls == []
    r, calls = sandbox("rollback", env={"ENV": "prod", "CONFIRM": "yes"})
    assert r.returncode == 0
    assert all("-n runwatch-prod" in c for c in calls)


def test_rollback_all_in_order(sandbox):
    r, calls = sandbox("rollback", env={"ENV": "dev"})
    assert r.returncode == 0, r.stderr
    p = prefix(sandbox)
    expected = []
    for n in ALL:
        expected += [
            f"{p} rollout history deployment/{n}",
            f"{p} rollout undo deployment/{n}",
            f"{p} rollout status deployment/{n} --timeout=180s",
        ]
    assert calls == expected
    assert "revert PR" in r.stdout
    assert "postgres" not in "".join(calls)


def test_rollback_single_with_revision(sandbox):
    r, calls = sandbox(
        "rollback", env={"ENV": "dev", "DEPLOY": "worker", "REVISION": "3"}
    )
    assert r.returncode == 0
    assert [c.split(" ", 6)[-1] for c in calls] == [
        "rollout history deployment/worker",
        "rollout undo deployment/worker --to-revision=3",
        "rollout status deployment/worker --timeout=180s",
    ]


def test_rollback_refusals(sandbox):
    r, calls = sandbox("rollback", env={"ENV": "dev", "DEPLOY": "postgres"})
    assert r.returncode != 0 and calls == []
    r, calls = sandbox("rollback", env={"ENV": "dev", "REVISION": "2"})
    assert r.returncode != 0 and calls == []


def test_rollback_skips_deployment_without_history(sandbox):
    r, calls = sandbox(
        "rollback",
        env={
            "ENV": "dev",
            "STUB_FAIL": "undo deployment/frontend=error: no rollout history found",
        },
    )
    assert r.returncode == 0, r.stderr
    assert "frontend: skipped: no previous revision" in r.stdout
    assert any("rollout undo deployment/agent" in c for c in calls)
    assert not any("rollout status deployment/frontend" in c for c in calls)


def test_rollback_other_failure_stops(sandbox):
    r, calls = sandbox(
        "rollback",
        env={"ENV": "dev", "STUB_FAIL": "undo deployment/worker=error: boom"},
    )
    assert r.returncode != 0
    assert not any("deployment/frontend" in c for c in calls)


def test_break_worker(sandbox):
    r, calls = sandbox("break-worker")
    assert r.returncode == 0
    assert calls == [f"{prefix(sandbox)} scale deployment/worker --replicas=0"]
    assert "JobsStalled" in r.stdout and "QueueWaitHigh" in r.stdout


def test_bad_release_order(sandbox):
    r, calls = sandbox("bad-release")
    assert r.returncode == 0, r.stderr
    changes = [c for c in calls if " get " not in c]
    p = prefix(sandbox)
    assert changes == [
        f"{p} annotate deployment/worker "
        "kubernetes.io/change-cause=bad-release drill: WORKER_FAULT=fail_all "
        "--overwrite",
        f"{p} set env deployment/worker WORKER_FAULT=fail_all",
    ]
    assert "JobFailureRateHigh" in r.stdout


def test_bad_release_refuses_scaled_down_worker(sandbox):
    r, calls = sandbox("bad-release", env={"STUB_REPLICAS": "0"})
    assert r.returncode != 0
    assert "run make heal first" in r.stderr
    assert not any("set env" in c for c in calls)


def test_bad_release_twice(sandbox):
    r, calls = sandbox("bad-release", env={"STUB_FAULT": "fail_all"})
    assert r.returncode == 0
    assert "already set, no new revision" in r.stdout
    assert not any("set env" in c for c in calls)


def test_heal(sandbox):
    for _ in range(2):
        r, calls = sandbox("heal")
        assert r.returncode == 0
    p = prefix(sandbox)
    assert calls[-4:] == [
        f"{p} annotate deployment/worker "
        "kubernetes.io/change-cause=heal: drill state cleared --overwrite",
        f"{p} set env deployment/worker WORKER_FAULT-",
        f"{p} scale deployment/worker --replicas=1",
        f"{p} rollout status deployment/worker --timeout=120s",
    ]
    assert "does not requeue" in r.stdout


@pytest.mark.parametrize("script", ["break-worker", "bad-release", "heal"])
def test_drills_refuse_prod(sandbox, script):
    r, calls = sandbox(script, env={"ENV": "prod"})
    assert r.returncode != 0 and calls == []


@pytest.mark.parametrize("script", SCRIPTS)
def test_missing_kubeconfig(sandbox, script, tmp_path):
    r, calls = sandbox(script, env={"ENV": "dev"}, kubeconfig=tmp_path / "nope")
    assert r.returncode != 0
    assert "run make kind-up first" in r.stderr
    assert calls == []


@pytest.mark.parametrize("script", SCRIPTS)
def test_dry_run(sandbox, script, tmp_path):
    r, calls = sandbox(
        script, env={"ENV": "dev", "DRY_RUN": "1"}, kubeconfig=tmp_path / "nope"
    )
    assert r.returncode == 0, r.stderr
    assert calls == []
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("DRY_RUN: ")]
    assert lines
    assert all("--context kind-runwatch -n runwatch-dev" in ln for ln in lines)
