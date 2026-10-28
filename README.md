# Runwatch

A small job run tracker with an on-call agent. Project 1 of AI Dev Tools
Zoomcamp 2026, built by coding agents from a public tracker and run at no
cost on one local VM.

**Demo: pending (owner step).** The recorded demo and a public URL do not
exist yet. The deployment is shown through the recording because the VM is not
reachable from the internet; until it is linked here, use the "Project
criteria checklist" below to see what is done and what is not.

Quick links: [Problem](#problem) |
[Functionality](#what-it-does-and-how-it-behaves) |
[Architecture](#architecture) | [Run locally](#run-it-locally-one-command) |
[Tests](#tests-one-command) | [Containers](#containerization) |
[Kubernetes](#kubernetes-kind-dev-and-prod) | [CI/CD](#cicd) |
[Observability](#observability) | [Alerts](#alerting-on-user-impact) |
[Responder agent](#responder-agent) | [AI-assisted process](#ai-assisted-process) |
[Checklist](#project-criteria-checklist)

## Problem

People who run batch workloads (simulations, data pipelines, model training)
submit many jobs and then have to watch them. Workers die and jobs sit
stuck in the queue, or a bad change makes jobs fail en masse. The user often
finds out hours later, and the fix is usually mechanical: restart the worker,
requeue the orphaned jobs, roll back the change.

Runwatch is a job run tracker in the spirit of Autosubmit. Users submit
simulated jobs and watch them in a web UI. The system measures what the user
experiences (how long jobs wait, whether they finish), alerts when that gets
worse, and a responder agent is woken by the alert to diagnose the cause and
apply a safe, allow-listed fix. Full specification, with acceptance criteria
`AC-1` to `AC-29`: [`_docs/SPEC.md`](_docs/SPEC.md).

## What it does and how it behaves

**User journeys** (web UI, `http://localhost:8080` under Compose):

1. Submit a job: name, duration (1-600 s), failure probability, mode
   (`normal` or `always_fail`), max attempts (1-10). The form validates
   before sending and shows API errors.
2. Watch the jobs page: state badges, filter by state, summary bar, refreshed
   every 3 s without a reload.
3. Open a job: attempts with failure cause, logs refreshed every 2 s while it
   runs, and requeue / cancel buttons shown only in states that allow them.

**Job lifecycle** (details and rules in the spec):

```
QUEUED -> RUNNING -> SUCCEEDED
            |-> QUEUED again   (attempt failed, attempts left: immediate retry)
            |-> FAILED         (attempt failed, no attempts left)
            '-> STALLED        (worker vanished, 15 s lease expired)
FAILED / STALLED -> QUEUED     (requeue)
QUEUED / RUNNING / STALLED -> CANCELLED
```

A separate worker process claims jobs from a Postgres queue (`FOR UPDATE SKIP
LOCKED`), runs the simulated workload, heartbeats, and reports. The API's
sweeper marks jobs with an expired lease `STALLED` but never requeues them:
that is deliberately left to a human or the agent, so the incident stays
visible. Every failed attempt has a cause, `workload` (what the user asked
for) or `infra` (anything else). Alerts count only `infra`, so testing failure
handling never pages anyone.

**Alerts and the agent**: see [Alerting](#alerting-on-user-impact) and
[Responder agent](#responder-agent). The two demo incidents are a dead worker
(`make break-worker`) and a bad release that makes every attempt fail
(`make bad-release`); `make heal` undoes either by hand.

## Architecture

```mermaid
flowchart LR
  Browser --> FE[Frontend nginx + React]
  FE -->|/api/v1| API[API FastAPI]
  LG[Load generator] --> API
  W[Worker] -->|/internal/v1| API
  API --> PG[(PostgreSQL 16)]
  API & W & LG & AG[Responder agent] -->|OTLP| OC[OTel Collector]
  OC --> PR[Prometheus]
  OC --> TE[Tempo]
  OC --> LO[Loki]
  PR --> GR[Grafana]
  TE --> GR
  LO --> GR
  PR -->|alert rules| AM[Alertmanager]
  AM -->|webhook + bearer token| AG
  AG -->|tools| API
  AG -->|read| LO
  AG -.->|kubectl-style rollout, scale; kind only| K8S[Kubernetes API]
```

| Component | Tech | Code / config |
| --- | --- | --- |
| Frontend | React + Vite, served by nginx | [`frontend/`](frontend/) |
| API | FastAPI, SQLAlchemy, Alembic; sweeper; `/healthz`, `/readyz` | [`app/runwatch/api/`](app/runwatch/api/), [`alembic/`](alembic/) |
| Worker | Python process, same image | [`app/runwatch/worker/`](app/runwatch/worker/) |
| Load generator | Python process, one 10 s job every 20 s by default | [`app/runwatch/loadgen.py`](app/runwatch/loadgen.py) |
| Database | PostgreSQL 16 (jobs, attempts, log lines) | [`app/runwatch/models.py`](app/runwatch/models.py) |
| Observability | OTel Collector, Prometheus, Tempo, Loki, Grafana, Alertmanager | [`observability/`](observability/) |
| Responder agent | Python + Claude tool-use loop, webhook receiver | [`app/runwatch/agent/`](app/runwatch/agent/) |
| Kubernetes | Kustomize base + `dev` / `prod` overlays, kind | [`k8s/`](k8s/), [`deploy/`](deploy/) |

The one path from alert to agent action: a Prometheus rule fires, Alertmanager
routes it by its `env` label to that environment's agent webhook
(`POST /webhook/alertmanager`, bearer token), the agent runs its tool loop,
and its only write powers are the allow-listed tools below.

## Run it locally (one command)

Prerequisites: Docker with Compose. No API key is needed.

```
cp .env.example .env        # optional; every value has a default in compose.yaml
docker compose up --build
```

| What | URL |
| --- | --- |
| Runwatch UI (API under `/api/v1`) | http://localhost:8080 |
| Grafana (no login, provisioned "Runwatch" dashboard) | http://localhost:3001 |
| Prometheus | http://localhost:9091 |

Compose starts Postgres, runs the migration, then API, worker, load
generator, frontend, the observability stack and the responder agent. It works
with or without `ANTHROPIC_API_KEY` (see [Responder agent](#responder-agent)).
Stop it with `docker compose stop`. Never use `docker compose down -v`: it
deletes the data volumes. Compose has no kind, no dev/prod, `env` is `local`,
and the agent has no Kubernetes access, so it can diagnose and requeue but
cannot change deployments. Compose and the kind cluster are not meant to run
together (the VM cannot hold both). All settings are listed in
[`.env.example`](.env.example).

## Tests (one command)

```
uv sync && (cd frontend && npm ci)
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/runwatch_wt1 make test
```

`make test` refuses to run without `DATABASE_URL`. The integration tests empty
the `jobs`, `attempts` and `log_lines` tables of the database it names, so
point it at a database of your own. It runs, in order:

| Target | Covers |
| --- | --- |
| `make test-unit` | pytest, no database: state machine, validation, lease and sweeper logic, stats, worker, load generator, telemetry, agent tool rules / loop / run control / reports, manifests, deploy and drill scripts ([`tests/unit/`](tests/unit/)) |
| `make test-frontend` | typecheck and Vitest: form validation, badges, conditional buttons, API errors ([`frontend/src/`](frontend/src/)) |
| `make test-integration` | real Postgres, real API and worker processes: AC-3 to AC-12, concurrent claim, restarts, telemetry privacy ([`tests/integration/`](tests/integration/)) |

Also: `make lint` (ruff), and `make test-alerts`, which checks the Prometheus
config and alert rules and runs `promtool test rules` against
[`observability/tests/alerts_test.yaml`](observability/tests/alerts_test.yaml)
inside the Compose Prometheus container (start it first with
`docker compose up -d prometheus`). CI runs all of this on every pull request
and fails if any test is skipped.

## Containerization

The whole system, database included, starts with the single
`docker compose up --build` above ([`compose.yaml`](compose.yaml)). One Python
image ([`Dockerfile`](Dockerfile), non-root user) serves the migration, API,
worker, load generator and agent, which differ only by command; the frontend
has its own image ([`frontend/Dockerfile`](frontend/Dockerfile)). Every
service has a memory limit and the app services do not depend on the
observability services, so a telemetry outage does not stop the app.

## Kubernetes (kind, dev and prod)

One single-node kind cluster, `runwatch`, on the VM, with three namespaces:
`runwatch-dev` and `runwatch-prod` (app, load generator, collector, agent each)
and `runwatch-observability` (one shared Prometheus, Tempo, Loki, Grafana,
Alertmanager). Manifests are Kustomize: [`k8s/base/`](k8s/base/) plus
[`k8s/overlays/dev`](k8s/overlays/dev) and
[`k8s/overlays/prod`](k8s/overlays/prod); details in
[`k8s/README.md`](k8s/README.md) and
[`observability/k8s/README.md`](observability/k8s/README.md).

```
docker compose stop                  # Compose and kind never run together
make kind-up                         # create or start the cluster
make k8s-secrets ENV=dev             # once per environment; also ENV=prod
make deploy ENV=dev                  # images of HEAD from GHCR; also ENV=prod
make k8s-validate                    # offline: render both overlays and check them
make kind-down                       # stop the cluster (never delete it)
```

The UI is routed at `http://dev.localhost/` and `http://prod.localhost/`;
Grafana and Prometheus are reached with `kubectl port-forward`, as in
[`observability/k8s/README.md`](observability/k8s/README.md). Runbooks:
[`kind-cluster.md`](docs/runbooks/kind-cluster.md),
[`deploy.md`](docs/runbooks/deploy.md),
[`rollback.md`](docs/runbooks/rollback.md),
[`self-hosted-runner.md`](docs/runbooks/self-hosted-runner.md).

Status: the manifests, `scripts/deploy.sh` and the runbooks are merged and the
dev and observability stack were verified by hand on the local kind cluster
(deploy, ingress, telemetry). The kind deployment is not reachable from the
internet, so there is no public URL.

## CI/CD

| Workflow | Trigger | What it does |
| --- | --- | --- |
| [`ci.yaml`](.github/workflows/ci.yaml) | every pull request | `lint`; `backend` (unit + integration tests against Postgres 16, skipped tests fail the job); `frontend` (typecheck + tests); a final "CI passed" gate |
| [`cd.yaml`](.github/workflows/cd.yaml) | push to `main` | runs CI, builds and pushes both images to GHCR tagged with the commit SHA, deploys to **dev** automatically, then deploys to **prod** only after a reviewer approves the `prod` GitHub environment |
| `cd.yaml` | manual dispatch | redeploys the images of a chosen earlier SHA on `main` to `dev` or `prod` (rollback path 2) |

The deploy jobs run on a self-hosted runner on the VM, because only the VM
reaches the kind cluster; CD never triggers on pull requests, so outside code
never runs there. **Rollback**: `make rollback ENV=<env>` (`kubectl rollout
undo` of the six Deployments) or the SHA redeploy above, both in
[`docs/runbooks/rollback.md`](docs/runbooks/rollback.md); after either, `main`
needs a revert PR.

Honest status:

- CI (the PR part) is in use on this repository.
- CD is written, unit-tested ([`tests/unit/test_cd_workflow.py`](tests/unit/test_cd_workflow.py),
  [`tests/unit/test_deploy_script.py`](tests/unit/test_deploy_script.py)) and
  its deploy script was run by hand against the local kind cluster. The CD
  workflow does start on every merge to `main`: as of 2026-10-28 its `ci` and
  `build` jobs (tests, then both images pushed to GHCR) complete successfully,
  but `deploy-dev` stays pending because no self-hosted runner is registered
  yet. **No deploy has been done by CD**: it needs the owner to register the
  runner and create the `dev` / `prod` GitHub environments
  ([issue #73](https://github.com/coalesce8/ai-dev-tools-zoomcamp-project1/issues/73),
  [`self-hosted-runner.md`](docs/runbooks/self-hosted-runner.md)). The prod
  manual-approval path is therefore described, not yet demonstrated.
- The "Exercised rollbacks" section of the runbook is still empty; the first
  live rollback is planned in
  [issue #37](https://github.com/coalesce8/ai-dev-tools-zoomcamp-project1/issues/37).

## Observability

The key journey is submit, start, finish.

- **Traces**: `POST /jobs` starts the submit trace and the job stores its
  context. Each attempt is its own trace started by the worker, with a span
  link to the submit span; heartbeat and result requests propagate it to the
  API. Spans carry `job.id` and `attempt.number`.
- **Logs**: structured JSON lines with `trace_id`, `span_id` and `job_id`, so
  Grafana jumps from a trace to its log lines in Loki.
- **Metrics** (exported every 10 s, all from the API so they stay correct with
  zero workers): jobs submitted / finished, attempts finished by outcome and
  cause, queue depth, oldest queued age, stalled jobs, queue wait and run
  duration histograms, HTTP server metrics. The full table is in the
  [spec](_docs/SPEC.md#observability).
- **Stack**: all processes export OTLP to the OTel Collector, which feeds
  Prometheus (metrics), Tempo (traces) and Loki (logs); Grafana shows them with
  the provisioned dashboard
  [`runwatch.json`](observability/grafana/dashboards/runwatch.json). Config in
  [`observability/`](observability/).
- **No customer data in telemetry**: jobs are identified by `job_id` only;
  job names, log content and secrets never appear in spans, metric labels or
  telemetry logs. This is checked by
  [`tests/integration/test_telemetry_privacy.py`](tests/integration/test_telemetry_privacy.py)
  (AC-18).

## Alerting on user impact

Rules live in [`observability/rules/runwatch.yaml`](observability/rules/runwatch.yaml)
(evaluated every 15 s, aggregated by `env`) and are unit-tested with `promtool`
([`alerts_test.yaml`](observability/tests/alerts_test.yaml), `make test-alerts`).

| Alert | Fires when | For | What the user experiences |
| --- | --- | --- | --- |
| `QueueWaitHigh` | oldest queued job older than 120 s | 1 m | "My job is not starting." |
| `JobsStalled` | at least one job is `STALLED` | 1 m | "My job silently died." |
| `JobFailureRateHigh` | over 50% of attempts in the last 5 m failed with cause `infra`, with at least 5 attempts | 1 m | "My jobs started failing." |
| `RunwatchApiDown` | the API's metrics are absent for 2 m (one rule per environment) | 1 m | "The app is down." |

CPU and memory graphs are not alerted on. Alertmanager
([`observability/alertmanager.yaml`](observability/alertmanager.yaml)) routes
by `env` to the matching agent webhook. Failures a user causes on purpose
(`always_fail`) are `workload` failures and do not fire `JobFailureRateHigh`
(AC-20). The default load generator rate (one 10 s job per 20 s) keeps one
worker at about half load so no alert fires on its own; the capacity
measurements are in [PR #97](https://github.com/coalesce8/ai-dev-tools-zoomcamp-project1/pull/97).
The live alert timings of the two drills (AC-19) are not measured yet; that is
part of issue #37.

## Responder agent

A small FastAPI service ([`app/runwatch/agent/`](app/runwatch/agent/)) that
receives Alertmanager webhooks (`POST /webhook/alertmanager`, bearer token
`AGENT_WEBHOOK_TOKEN`) and runs a Claude tool-use loop (model from
`AGENT_MODEL`).

- **Tools** (declared once in `tools.py`, twelve in all). Read: `get_stats`,
  `list_jobs`, `get_job_logs`, `k8s_get_pods`, `k8s_get_events`,
  `k8s_rollout_history`, `query_loki`. Write: `k8s_rollout_restart` and
  `k8s_rollout_undo` (worker or api), `k8s_scale` (worker only, 1 to 3
  replicas), `requeue_stalled_jobs`, `requeue_failed_jobs` (only `infra`
  failures in a time window).
- **Tool rules, enforced in code** (not by the prompt): a tool outside the
  list, a deployment outside the allowed set, `k8s_scale` outside 1-3 and any
  namespace argument are refused; the namespace comes from configuration, never
  from the model. The Kubernetes Role is namespaced and cannot delete or run
  shell. Tool results are wrapped as untrusted data, since job names and logs
  are user text.
- **Run control**: one run per alert fingerprint, a cooldown after it ends
  (`AGENT_COOLDOWN_S`), and repeated webhooks are recorded in the existing
  incident. Caps per run: 20 tool calls, 3 write actions, 200k tokens. After
  acting it re-checks the alert condition for up to 10 minutes and otherwise
  ends `escalated`.
- **Incident reports**: every run writes a directory with `alert.json`,
  `status.json`, a Markdown `response.md` and more
  ([`docs/incident-files.md`](docs/incident-files.md)), viewable at the
  agent's `GET /incidents` and `GET /incidents/<id>`. Secrets and job content
  are redacted from reports. After a rollback the report says `main` needs a
  revert PR.

**Honest status.** The agent, its tool rules, the loop, run control, incident
reports and the alert-to-agent pipeline (webhook, cooldown, report) are
implemented and tested with **fake model clients**
([`tests/unit/test_agent_loop.py`](tests/unit/test_agent_loop.py),
[`test_agent_tool_rules.py`](tests/unit/test_agent_tool_rules.py),
[`test_agent_runs.py`](tests/unit/test_agent_runs.py)). This project is run at
no cost and **no `ANTHROPIC_API_KEY` exists or will be used**, so without a key
every run ends `skipped_no_model` and still writes a report. The agent has
**not** diagnosed or fixed a real incident. Running the two drills with a live
model (`make break-worker`, `make bad-release`, `make heal`; AC-21 and AC-22)
is an owner-run step that has not been run, so this part of the on-call agent
criterion is implemented but not demonstrated.

## AI-assisted process

The project was built by Claude Code agents working from a public issue
tracker, with roles defined in the repository:

| Role | Does | Definition |
| --- | --- | --- |
| Orchestrator | the main session; schedules independent issues, gives each its own git worktree and database, sends finished work to QA, merges approved PRs one at a time; never implements | [`_docs/process.md`](_docs/process.md) |
| PM | grooms an issue with the template: goal, AC references, checkable acceptance criteria, out of scope, constraints, dependencies; writes no code | [`_docs/team/pm.md`](_docs/team/pm.md) |
| Software engineer | implements one groomed issue on a branch `issue-<n>-<slug>`, with tests, and opens one PR | [`_docs/team/software-engineer.md`](_docs/team/software-engineer.md) |
| QA engineer | a fresh agent per PR checks it against the issue's criteria, runs the tests, posts PASS or FAIL; changes no code | [`_docs/team/qa-engineer.md`](_docs/team/qa-engineer.md) |

The workflow: the spec ([`_docs/SPEC.md`](_docs/SPEC.md)) came first and issues
reference its `AC-` ids; each issue is groomed with
[`_docs/task-template.md`](_docs/task-template.md); one issue, one worktree,
one branch, one PR; QA is a new agent that never saw the implementation; review
findings go back to the engineer; the orchestrator merges. Roles are also
Claude Code subagents in [`.claude/agents/`](.claude/agents/).

Context management: [`AGENTS.md`](AGENTS.md) (imported by
[`CLAUDE.md`](CLAUDE.md)) describes the project, commands and rules and points
to the modular docs: [`_docs/process.md`](_docs/process.md),
[`_docs/team/`](_docs/team/), [`_docs/task-template.md`](_docs/task-template.md)
and the [runbooks](docs/runbooks/).

Evidence the agents used the tracker: the public
[issue tracker](https://github.com/coalesce8/ai-dev-tools-zoomcamp-project1/issues)
(issues with acceptance criteria, labels `mvp` / `groomed` / `serial`, and QA
verdict comments) and the
[pull request list](https://github.com/coalesce8/ai-dev-tools-zoomcamp-project1/pulls?q=is%3Apr)
(one PR per issue, with QA verdict comments). As of 2026-10-28 the GitHub API
shows 39 merged pull requests, 37 closed issues and 24 open issues; the counts
change as work continues.

## Cost and limitations

- Cost is zero: one local VM, kind, local images, free GitHub runners, GHCR for
  public repositories. No paid service is used.
- Single tenant; the public write endpoints (submit, requeue, cancel) are
  unauthenticated, an accepted risk for the demo. Internal endpoints are never
  routed through the ingress.
- The VM is not reachable from the internet; one kind node; no database
  migration rollback.
- Not done yet: the demo recording and public URL, the first CD run, the live
  drills and their incident reports (see the checklist).

## Project criteria checklist

The 12 criteria of the course ([`_docs/old/PROJECT.md`](_docs/old/PROJECT.md),
24 points, 10 to pass), with where each is satisfied and its honest status.

| # | Criterion | Where | Status |
| --- | --- | --- | --- |
| 1 | Problem description | [Problem](#problem), [Functionality](#what-it-does-and-how-it-behaves) | Done |
| 2 | Product specification | [`_docs/SPEC.md`](_docs/SPEC.md); [public tracker](https://github.com/coalesce8/ai-dev-tools-zoomcamp-project1/issues) with acceptance criteria | Done |
| 3 | Agent team specification | [`_docs/team/`](_docs/team/), [`_docs/process.md`](_docs/process.md), [AI-assisted process](#ai-assisted-process) | Done |
| 4 | Context management | [`AGENTS.md`](AGENTS.md) pointing to process, team and runbook docs | Done |
| 5 | Application | [`app/`](app/), [`frontend/`](frontend/); `docker compose up --build` | Done |
| 6 | Testing | [Tests](#tests-one-command): unit, frontend and integration with `make test` | Done |
| 7 | Containerization | [`compose.yaml`](compose.yaml), [`Dockerfile`](Dockerfile) | Done |
| 8 | Deployment | [Kubernetes](#kubernetes-kind-dev-and-prod), [`deploy.md`](docs/runbooks/deploy.md); deployed to a local kind cluster, no public URL | Partial: steps and manifests done and checked on kind by hand; **demo recording pending (owner step)** |
| 9 | CI/CD | [CI/CD](#cicd), [`ci.yaml`](.github/workflows/ci.yaml), [`cd.yaml`](.github/workflows/cd.yaml), [`rollback.md`](docs/runbooks/rollback.md) | CI in use; CD to dev with manual-approval prod written and tested; CI and image build run on every merge, **deploy jobs not yet run (needs self-hosted runner, #73)** |
| 10 | Observability | [Observability](#observability), [`observability/`](observability/) | Done (traces, metrics and logs share a trace id; viewable in Grafana) |
| 11 | Alerting on user impact | [Alerting](#alerting-on-user-impact), [`runwatch.yaml`](observability/rules/runwatch.yaml) | Done (four rules, promtool-tested); live drill timings pending (#37) |
| 12 | On-call agent | [Responder agent](#responder-agent), [`app/runwatch/agent/`](app/runwatch/agent/) | Implemented and tested with fake model clients; **no live-model run, no real incident fixed**: owner step |
