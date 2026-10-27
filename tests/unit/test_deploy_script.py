"""scripts/deploy.sh: argument checks and the offline dry run."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deploy.sh"
SHA = "0123456789abcdef0123456789abcdef01234567"

needs_kubectl = pytest.mark.skipif(
    shutil.which("kubectl") is None,
    reason="kubectl is not on PATH (kustomize is built into it)",
)


def run(*args, dry=False):
    env = {**os.environ, "DRY_RUN": "1" if dry else ""}
    return subprocess.run(
        [str(SCRIPT), *args], cwd=ROOT, env=env, capture_output=True, text=True
    )


def git_status():
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
    ).stdout


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("staging", SHA),
        ("dev",),
        ("dev", "nothex"),
        ("dev", SHA.upper()),
        ("prod", SHA[:39]),
    ],
)
def test_bad_arguments_exit_nonzero_with_a_message(args):
    result = run(*args)
    assert result.returncode != 0
    assert result.stderr.strip()


def test_script_has_no_forbidden_commands():
    text = (
        SCRIPT.read_text()
        + (ROOT / "scripts" / "k8s-alertmanager-secret.sh").read_text()
    )
    for pattern in (
        r"agent-relay",
        r"kubectl delete",
        r"\brm\b",
        r"compose down",
        r"set -x",
    ):
        assert not re.search(pattern, text), pattern


@needs_kubectl
@pytest.mark.parametrize("env", ["dev", "prod"])
def test_dry_run_renders_the_sha_and_leaves_the_tree_clean(env):
    before = git_status()
    result = run(env, SHA, dry=True)
    assert result.returncode == 0, result.stderr
    assert f"ghcr.io/coalesce8/runwatch-app:{SHA}" in result.stdout
    assert f"ghcr.io/coalesce8/runwatch-frontend:{SHA}" in result.stdout
    assert git_status() == before

    rendered = subprocess.run(
        ["kubectl", "kustomize", str(ROOT / ".deploy" / "k8s" / "overlays" / env)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    images = {}
    for doc in yaml.safe_load_all(rendered):
        if doc and doc["kind"] == "Deployment":
            spec = doc["spec"]["template"]["spec"]
            for c in spec.get("initContainers", []) + spec["containers"]:
                images[(doc["metadata"]["name"], c["name"])] = c["image"]
    app = f"ghcr.io/coalesce8/runwatch-app:{SHA}"
    assert images[("api", "api")] == app
    assert images[("worker", "worker")] == app
    assert (
        images[("frontend", "frontend")] == f"ghcr.io/coalesce8/runwatch-frontend:{SHA}"
    )

    committed = (ROOT / "k8s" / "overlays" / env / "kustomization.yaml").read_text()
    assert committed.count("newTag: local") == 2


@needs_kubectl
def test_prod_dry_run_does_not_apply_observability():
    out = run("prod", SHA, dry=True).stdout
    assert "kubectl apply -k observability" not in out
    assert "prod never applies observability/" in out
