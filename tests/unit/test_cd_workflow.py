"""Structural checks on .github/workflows/cd.yaml (offline)."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / ".github" / "workflows" / "cd.yaml"
TEXT = PATH.read_text()
WF = yaml.safe_load(TEXT)
# PyYAML reads the key `on` as the boolean True.
ON = WF.get("on", WF.get(True))
JOBS = WF["jobs"]


def test_name_and_triggers():
    assert WF["name"] == "CD"
    assert set(ON) == {"push", "workflow_dispatch"}
    assert ON["push"] == {"branches": ["main"]}
    inputs = ON["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"environment", "sha"}
    assert inputs["environment"]["type"] == "choice"
    assert inputs["environment"]["options"] == ["dev", "prod"]
    assert inputs["environment"]["required"] is True
    assert inputs["sha"]["type"] == "string"
    assert not inputs["sha"].get("required")
    for forbidden in ("pull_request", "pull_request_target", "workflow_run"):
        assert forbidden not in ON


def test_only_the_github_token_secret():
    names = re.findall(r"secrets\.(\w+)", TEXT)
    assert names and set(names) == {"GITHUB_TOKEN"}


def test_permissions():
    assert WF["permissions"] == {"contents": "read"}
    assert JOBS["ci"]["permissions"] == {"contents": "read"}
    assert JOBS["build"]["permissions"] == {"contents": "read", "packages": "write"}
    for name in ("deploy-dev", "deploy-prod"):
        assert JOBS[name]["permissions"] == {"contents": "read", "packages": "read"}
    # packages: write appears once, in build.
    assert len(re.findall(r"packages: write", TEXT)) == 1
    assert not re.findall(r"(?:write-all|id-token|actions: write)", TEXT)


def test_actions_are_pinned_to_full_shas():
    uses = re.findall(r"^\s*-?\s*uses: (\S+)(.*)$", TEXT, re.M)
    assert uses
    for ref, rest in uses:
        if ref.startswith("./"):
            continue
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), ref
        assert re.search(r"# v\d", rest), f"{ref} lacks its release tag comment"


def test_ci_job_calls_the_reusable_workflow_on_push_only():
    ci = JOBS["ci"]
    assert ci["uses"] == "./.github/workflows/ci.yaml"
    assert "github.event_name == 'push'" in ci["if"]


def test_build_job():
    build = JOBS["build"]
    assert build["needs"] == "ci"
    assert build["runs-on"] == "ubuntu-24.04"
    assert "!cancelled()" in build["if"]
    assert "needs.ci.result == 'success'" in build["if"]
    assert "needs.ci.result == 'skipped'" in build["if"]
    pushes = [
        s
        for s in build["steps"]
        if str(s.get("uses", "")).startswith("docker/build-push-action@")
    ]
    assert len(pushes) == 2
    by_file = {s["with"]["file"]: s["with"] for s in pushes}
    assert set(by_file) == {"Dockerfile", "frontend/Dockerfile"}
    for w in by_file.values():
        assert w["push"] is True
        assert "\n" not in w["tags"].strip()
        assert w["tags"].endswith(":${{ steps.sha.outputs.sha }}")
        assert (
            "org.opencontainers.image.source=https://github.com/coalesce8/ai-dev-tools-zoomcamp-project1"
            in w["labels"]
        )
        assert "type=gha" in w["cache-from"] and "type=gha" in w["cache-to"]
    assert WF["env"]["IMAGE_APP"] == "ghcr.io/coalesce8/runwatch-app"
    assert WF["env"]["IMAGE_FRONTEND"] == "ghcr.io/coalesce8/runwatch-frontend"
    assert "latest" not in TEXT.replace("ubuntu-latest", "")
    login = [
        s
        for s in build["steps"]
        if str(s.get("uses", "")).startswith("docker/login-action@")
    ]
    assert login and login[0]["with"]["registry"] == "ghcr.io"
    assert "imagetools inspect" in TEXT


def test_deploy_job_chain_and_guards():
    dev, prod = JOBS["deploy-dev"], JOBS["deploy-prod"]
    assert dev["needs"] == "build"
    assert "needs.build.result == 'success'" in dev["if"]
    assert set(prod["needs"]) == {"build", "deploy-dev"}
    assert "needs.deploy-dev.result == 'success'" in prod["if"]
    assert dev["environment"] == "dev" and prod["environment"] == "prod"
    assert "inputs.environment == 'dev'" in dev["if"]
    assert "inputs.environment == 'prod'" in prod["if"]
    for job in (dev, prod):
        assert "github.ref == 'refs/heads/main'" in job["if"]
        assert job["runs-on"] == ["self-hosted", "runwatch-vm"]
    assert "github.event_name == 'push'" in dev["if"]
    assert "github.event_name == 'push'" in prod["if"]


def test_self_hosted_only_in_the_two_deploy_jobs():
    for name, job in JOBS.items():
        runs_on = job.get("runs-on", [])
        runs_on = [runs_on] if isinstance(runs_on, str) else runs_on
        assert ("self-hosted" in runs_on) == (name in ("deploy-dev", "deploy-prod")), (
            name
        )
    assert len(re.findall(r"self-hosted", TEXT.split("jobs:", 1)[1])) == 2


def test_deploy_concurrency_never_cancels():
    for env in ("dev", "prod"):
        conc = JOBS[f"deploy-{env}"]["concurrency"]
        assert conc == {"group": f"deploy-{env}", "cancel-in-progress": False}


def test_deploy_steps():
    for env in ("dev", "prod"):
        steps = JOBS[f"deploy-{env}"]["steps"]
        checkout = steps[0]
        assert checkout["uses"].startswith("actions/checkout@")
        assert checkout["with"] == {
            "ref": "${{ github.sha }}",
            "fetch-depth": 0,
            "persist-credentials": False,
        }
        runs = "\n".join(s.get("run", "") for s in steps)
        assert "git merge-base --is-ancestor" in runs and "origin/main" in runs
        assert runs.index("docker login ghcr.io") < runs.index(
            f"scripts/deploy.sh {env}"
        )
        assert (
            "GITHUB_STEP_SUMMARY" in runs
            and "rollout history deployment/worker" in runs
        )
        assert "tail -n 3" in runs
