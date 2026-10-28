"""Structural checks on the rendered Kustomize overlays (offline, no cluster)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    shutil.which("kubectl") is None,
    reason="kubectl is not on PATH (kustomize is built into it)",
)

ENVS = ("dev", "prod")
DEPLOYMENTS = ["api", "worker", "loadgen", "frontend", "otel-collector", "agent"]
LIMITS_MI = {
    "postgres": 192,
    "api": 192,
    "migrate": 192,
    "worker": 96,
    "loadgen": 96,
    "frontend": 48,
    "otel-collector": 256,
    "agent": 128,
}
HOST_PORT_8000 = ["--host", "0.0.0.0", "--port", "8000"]
HOST_PORT_8090 = ["--host", "0.0.0.0", "--port", "8090"]


def mi(value: str) -> int:
    assert value.endswith("Mi"), value
    return int(value[:-2])


def render_text(path: str) -> str:
    result = subprocess.run(
        ["kubectl", "kustomize", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def render(env: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(render_text(f"k8s/overlays/{env}")) if d]


@pytest.fixture(scope="module", params=ENVS)
def env_docs(request):
    return request.param, render(request.param)


def of_kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def by_name(docs, kind, name):
    (found,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return found


def pod_specs(docs):
    for d in docs:
        if d["kind"] == "Deployment" or d["kind"] == "StatefulSet":
            yield d["metadata"]["name"], d["spec"]["template"]["spec"]


def test_base_and_overlays_render_and_base_has_no_namespace():
    base = render_text("k8s/base")
    assert "namespace:" not in re.sub(r"fieldPath: metadata\.namespace", "", base)
    for env in ENVS:
        assert render_text(f"k8s/overlays/{env}").strip()


def test_namespaces(env_docs):
    env, docs = env_docs
    ns = f"runwatch-{env}"
    other = "runwatch-prod" if env == "dev" else "runwatch-dev"
    assert by_name(docs, "Namespace", ns)
    for d in docs:
        if d["kind"] != "Namespace":
            assert d["metadata"]["namespace"] == ns, d["metadata"]["name"]
    assert other not in render_text(f"k8s/overlays/{env}")
    for d in docs:
        assert d["metadata"]["labels"]["env"] == env


def test_workloads_and_services(env_docs):
    _, docs = env_docs
    for name in DEPLOYMENTS:
        assert by_name(docs, "Deployment", name)["spec"]["replicas"] == 1
    assert by_name(docs, "StatefulSet", "postgres")["spec"]["replicas"] == 1
    expected = {
        "postgres": [5432],
        "api": [8000],
        "frontend": [8080],
        "otel-collector": [4317, 4318],
        "agent": [8090],
    }
    for name, ports in expected.items():
        svc = by_name(docs, "Service", name)
        assert [p["port"] for p in svc["spec"]["ports"]] == ports


def test_selectors_are_stable_across_overlays():
    rendered = {env: render(env) for env in ENVS}
    for name in DEPLOYMENTS:
        sel = [
            by_name(rendered[e], "Deployment", name)["spec"]["selector"] for e in ENVS
        ]
        assert sel[0] == sel[1]
        assert set(sel[0]["matchLabels"]) == {
            "app.kubernetes.io/name",
            "app.kubernetes.io/part-of",
        }
        assert sel[0]["matchLabels"]["app.kubernetes.io/name"] == name


def test_images(env_docs):
    _, docs = env_docs
    app = "ghcr.io/coalesce8/runwatch-app"
    for name, spec in pod_specs(docs):
        for c in spec.get("initContainers", []) + spec["containers"]:
            image = c["image"]
            assert ":" in image.rsplit("/", 1)[-1], image
            assert not image.endswith(":latest"), image
            assert c["imagePullPolicy"] == "IfNotPresent"
            if name in ("api", "worker", "loadgen", "agent"):
                assert image.startswith(app + ":")
    assert (
        by_name(docs, "StatefulSet", "postgres")["spec"]["template"]["spec"][
            "containers"
        ][0]["image"]
        == "postgres:16"
    )
    collector = by_name(docs, "Deployment", "otel-collector")
    assert (
        collector["spec"]["template"]["spec"]["containers"][0]["image"]
        == "otel/opentelemetry-collector-contrib:0.161.0"
    )
    front = by_name(docs, "Deployment", "frontend")
    assert front["spec"]["template"]["spec"]["containers"][0]["image"].startswith(
        "ghcr.io/coalesce8/runwatch-frontend:"
    )


def test_overlay_tags_are_on_their_own_lines():
    for env in ENVS:
        text = (ROOT / f"k8s/overlays/{env}/kustomization.yaml").read_text()
        assert len(re.findall(r"^\s+newTag: local$", text, re.M)) == 2


def test_commands(env_docs):
    _, docs = env_docs

    def cmd(name):
        spec = by_name(docs, "Deployment", name)["spec"]["template"]["spec"]
        return spec["containers"][0]["command"]

    assert (
        cmd("api")
        == ["uvicorn", "--factory", "runwatch.api.app:create_app"] + HOST_PORT_8000
    )
    assert cmd("worker") == ["python", "-m", "runwatch.worker"]
    assert cmd("loadgen") == ["python", "-m", "runwatch.loadgen"]
    assert (
        cmd("agent")
        == ["uvicorn", "--factory", "runwatch.agent.app:create_app"] + HOST_PORT_8090
    )


def test_memory_and_security(env_docs):
    _, docs = env_docs
    total = 0
    for name, spec in pod_specs(docs):
        for c in spec.get("initContainers", []) + spec["containers"]:
            res = c["resources"]
            req, lim = mi(res["requests"]["memory"]), mi(res["limits"]["memory"])
            assert req <= lim
            assert lim == LIMITS_MI[c["name"]], c["name"]
            assert "cpu" in res["requests"]
            assert "cpu" not in res["limits"]
        for c in spec["containers"]:
            total += mi(c["resources"]["limits"]["memory"])
        if name in ("api", "worker", "loadgen", "agent"):
            for c in spec.get("initContainers", []) + spec["containers"]:
                sc = c["securityContext"]
                assert sc["allowPrivilegeEscalation"] is False
                assert sc["capabilities"]["drop"] == ["ALL"]
                assert sc["seccompProfile"]["type"] == "RuntimeDefault"
    assert total == 1008
    assert total <= 1024


def env_of(container):
    return {e["name"]: e for e in container.get("env", [])}


def test_api_wiring(env_docs):
    _, docs = env_docs
    dep = by_name(docs, "Deployment", "api")
    spec = dep["spec"]["template"]["spec"]
    (migrate,) = spec["initContainers"]
    assert migrate["name"] == "migrate"
    assert migrate["command"] == ["alembic", "upgrade", "head"]
    api = spec["containers"][0]
    assert migrate["env"] == [
        e for e in api["env"] if e["name"] in ("POSTGRES_PASSWORD", "DATABASE_URL")
    ]
    names = [e["name"] for e in api["env"]]
    assert names.index("POSTGRES_PASSWORD") < names.index("DATABASE_URL")
    envs = env_of(api)
    assert envs["DATABASE_URL"]["value"] == (
        "postgresql+psycopg://postgres:$(POSTGRES_PASSWORD)@postgres:5432/runwatch"
    )
    assert envs["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "postgres",
        "key": "password",
    }
    assert api["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": 8000}
    assert api["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": 8000}
    # Telemetry outages must not gate the app.
    for name in ("api", "worker", "loadgen"):
        s = by_name(docs, "Deployment", name)["spec"]["template"]["spec"]
        blob = yaml.safe_dump(
            {
                "init": s.get("initContainers"),
                "probes": [
                    (c.get("readinessProbe"), c.get("livenessProbe"))
                    for c in s["containers"]
                ],
            }
        )
        assert "otel" not in blob
        assert "observability" not in blob


def test_other_wiring(env_docs):
    env, docs = env_docs
    pg = by_name(docs, "StatefulSet", "postgres")
    c = pg["spec"]["template"]["spec"]["containers"][0]
    e = env_of(c)
    assert e["POSTGRES_DB"]["value"] == "runwatch"
    assert e["POSTGRES_USER"]["value"] == "postgres"
    assert e["PGDATA"]["value"].startswith("/var/lib/postgresql/data/")
    assert "pg_isready" in c["readinessProbe"]["exec"]["command"]
    (claim,) = pg["spec"]["volumeClaimTemplates"]
    assert claim["metadata"]["name"] == "data"
    assert claim["spec"]["resources"]["requests"]["storage"] == "1Gi"

    for name in ("worker", "loadgen"):
        c = by_name(docs, "Deployment", name)["spec"]["template"]["spec"]["containers"][
            0
        ]
        e = env_of(c)
        assert e["RUNWATCH_API_URL"]["value"] == "http://api:8000"
        assert e["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"] == "http://otel-collector:4318"
        assert "WORKER_FAULT" not in e and "WORKER_ID" not in e
    fe = by_name(docs, "Deployment", "frontend")["spec"]["template"]["spec"][
        "containers"
    ][0]
    assert env_of(fe)["API_UPSTREAM"]["value"] == "http://api:8000"
    assert fe["readinessProbe"]["httpGet"] == {"path": "/", "port": 8080}

    col = by_name(docs, "Deployment", "otel-collector")["spec"]["template"]["spec"]
    assert env_of(col["containers"][0])["DEPLOYMENT_ENV"]["value"] == env
    mount = col["containers"][0]["volumeMounts"][0]
    assert mount["readOnly"] is True
    assert col["containers"][0]["args"] == [
        f"--config={mount['mountPath']}/config.yaml"
    ]
    cm_name = col["volumes"][0]["configMap"]["name"]
    assert re.fullmatch(r"otel-collector-config-\w+", cm_name)
    cm = by_name(docs, "ConfigMap", cm_name)
    cfg = yaml.safe_load(cm["data"]["config.yaml"])
    assert (
        cfg["processors"]["resource"]["attributes"][0]["value"]
        == "${env:DEPLOYMENT_ENV}"
    )
    assert set(cfg["service"]["pipelines"]) == {"metrics", "logs", "traces"}
    assert {x["endpoint"] for x in cfg["exporters"].values()} == {
        "http://prometheus.runwatch-observability.svc.cluster.local:9090/api/v1/otlp",
        "http://loki.runwatch-observability.svc.cluster.local:3100/otlp",
        "http://tempo.runwatch-observability.svc.cluster.local:4318",
    }
    assert "transform/env" in cfg["service"]["pipelines"]["metrics"]["processors"]


def test_agent_and_service_accounts(env_docs):
    env, docs = env_docs
    dep = by_name(docs, "Deployment", "agent")
    assert dep["spec"]["strategy"]["type"] == "Recreate"
    spec = dep["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "agent"
    c = spec["containers"][0]
    assert c["ports"][0]["containerPort"] == 8090
    assert c["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": 8090}
    assert c["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": 8090}
    e = env_of(c)
    assert e["AGENT_ENV"]["value"] == env
    assert e["AGENT_INCIDENTS_DIR"]["value"] == "/incidents"
    assert e["RUNWATCH_API_URL"]["value"] == "http://api:8000"
    assert e["AGENT_LOKI_URL"]["value"] == (
        "http://loki.runwatch-observability.svc.cluster.local:3100"
    )
    assert (
        e["AGENT_K8S_NAMESPACE"]["valueFrom"]["fieldRef"]["fieldPath"]
        == "metadata.namespace"
    )
    ref = e["AGENT_WEBHOOK_TOKEN"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "agent", "key": "webhook-token"}
    for var, key in (
        ("ANTHROPIC_API_KEY", "anthropic-api-key"),
        ("GITHUB_TOKEN", "github-token"),
    ):
        ref = e[var]["valueFrom"]["secretKeyRef"]
        assert ref == {"name": "agent", "key": key, "optional": True}
    pvc = by_name(docs, "PersistentVolumeClaim", "agent-incidents")
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "256Mi"
    for name, s in pod_specs(docs):
        if name != "agent":
            assert s["automountServiceAccountToken"] is False, name


def test_rbac(env_docs):
    env, docs = env_docs
    role = by_name(docs, "Role", "agent")
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["pods", "events"], "verbs": ["get", "list"]},
        {
            "apiGroups": ["apps"],
            "resources": ["deployments", "replicasets"],
            "verbs": ["get", "list"],
        },
        {
            "apiGroups": ["apps"],
            "resources": ["deployments", "deployments/scale"],
            "verbs": ["patch"],
        },
    ]
    rb = by_name(docs, "RoleBinding", "agent")
    assert rb["roleRef"]["kind"] == "Role" and rb["roleRef"]["name"] == "agent"
    assert rb["subjects"] == [
        {"kind": "ServiceAccount", "name": "agent", "namespace": f"runwatch-{env}"}
    ]
    kinds = {d["kind"] for d in docs}
    assert not kinds & {"ClusterRole", "ClusterRoleBinding", "Secret"}


def test_revision_history_and_strategy(env_docs):
    _, docs = env_docs
    for name in ("api", "worker", "loadgen", "frontend"):
        d = by_name(docs, "Deployment", name)
        assert d["spec"]["revisionHistoryLimit"] == 5
        assert "strategy" not in d["spec"]
    for d in of_kind(docs, "Deployment"):
        assert "kubernetes.io/change-cause" not in d["metadata"].get("annotations", {})
        assert "kubernetes.io/change-cause" not in d["spec"]["template"][
            "metadata"
        ].get("annotations", {})


def test_ingress(env_docs):
    env, docs = env_docs
    ing = by_name(docs, "Ingress", "runwatch")
    assert ing["spec"]["ingressClassName"] == "nginx"
    (rule,) = ing["spec"]["rules"]
    assert rule["host"] == f"{env}.localhost"
    paths = [
        (
            p["path"],
            p["pathType"],
            p["backend"]["service"]["name"],
            p["backend"]["service"]["port"]["number"],
        )
        for p in rule["http"]["paths"]
    ]
    assert paths == [
        ("/api/v1", "Prefix", "api", 8000),
        ("/", "Prefix", "frontend", 8080),
    ]


def test_no_credentials_in_k8s_dir():
    pattern = re.compile(r"password|token|api[_-]?key", re.I)
    for path in (ROOT / "k8s").rglob("*"):
        if not path.is_file() or path.name == "README.md":
            continue
        for line in path.read_text().splitlines():
            if pattern.search(line):
                # Only names of Secret keys and env vars, never a literal value.
                assert not re.search(
                    r"(value|password|token):\s*['\"]?[A-Za-z0-9]{12,}", line
                ), line
