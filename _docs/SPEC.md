# Runwatch specification

Status: draft v2. This document defines the problem, scope, behavior, and acceptance criteria for Runwatch, Project 1 of AI Dev Tools Zoomcamp 2026. It is written before the code; tasks in the tracker reference the acceptance criteria IDs below (`AC-…`).

## Problem

People who run batch workloads (simulations, data pipelines, model training) submit many jobs and then have to watch them. Jobs get stuck in the queue when workers die, or start failing en masse after a bad change. Today the user usually finds out hours later by checking a terminal, and the fix is often mechanical: restart the worker, requeue the orphaned jobs, roll back the change.

Runwatch is a small job run tracker in the spirit of Autosubmit. Users submit jobs and watch them in a web UI. The system measures what the user actually experiences (how long jobs wait, whether they finish), alerts when that degrades, and an on-call agent responds to the alert by diagnosing the cause and applying a safe fix.

## Scope

### In scope (v1)

- Submit a job, see it progress through its lifecycle, read its logs.
- Simulated workloads with configurable duration and failure behavior, executed by a separate worker process.
- Automatic retries for failed jobs, up to a per-job limit.
- Detection of jobs whose worker disappeared (lease expiry).
- Manual requeue and cancel from the UI.
- A load generator that submits a steady stream of healthy jobs, so user-impact signals always have traffic.
- Telemetry (traces, metrics, structured logs) for the submit-to-finish journey.
- Alerts on user impact, delivered to a responder agent that can fix the two demo incidents below.

### Out of scope (v1)

- Running real workloads or integrating with Slurm, PBS, or Autosubmit itself.
- Job dependencies, DAGs, schedules, or cron jobs.
- Detecting hung jobs (a process that is alive and heartbeating but makes no progress).
- Multiple users, authentication, or quotas. The app is single-tenant. The public write endpoints (submit, requeue, cancel) are unauthenticated; this is an accepted risk for the demo, and the internal endpoints are never exposed through the public ingress.
- An in-app notification system. Alerting is done by the observability stack (see "Two layers" below).

### Two layers, one loop

The product idea ("alert on stuck jobs, have an agent requeue them") and the course requirements (alert on user impact, on-call agent) are the same mechanism:

```
user-visible degradation  →  Prometheus alert rule  →  Alertmanager webhook
  →  responder agent  →  inspects k8s / API / logs  →  fixes  →  alert resolves
```

The app itself never auto-requeues stalled jobs or restarts workers. That is the agent's job, which keeps the incident visible and gives the agent real work.

## Components

| Component | Tech | Responsibility |
| --- | --- | --- |
| Frontend | React + Vite | Job list, job detail with live status and logs, submit form, requeue/cancel buttons. |
| API | FastAPI, SQLAlchemy, Alembic | Public REST API for jobs; internal worker and agent endpoints; lease sweeper; DB-derived gauges; `/healthz`, `/readyz`. |
| Worker | Python process (same codebase) | Claims queued jobs, runs the simulated workload, heartbeats, reports results. Scaled independently. |
| Load generator | Python process (same codebase) | Submits one `normal` job (`duration_s: 10`, `fail_probability: 0`) every `LOADGEN_INTERVAL_S` (default 10 s). |
| Database | PostgreSQL 16 | Jobs, attempts, log lines. Single source of truth; the queue is a table (`FOR UPDATE SKIP LOCKED`). |
| Observability | OTel Collector, Prometheus, Tempo, Loki, Grafana, Alertmanager | Collect, store, and view signals; evaluate alert rules. All services export over OTLP to the collector. |
| Responder agent | Python + Claude (Anthropic API) with tool use | Receives Alertmanager webhooks, diagnoses, applies allow-listed fixes, records an incident report. |

Everything runs locally with `docker compose up` and is deployed to Kubernetes with the same images.

### Starting points

Earlier homework is reused:

- `hw4/observability/`: collector, Prometheus, Tempo, Loki, and Grafana configuration with log-to-trace links.
- `hw4/incident-response/`: webhook responder skeleton and the per-incident directory layout (`alert.json`, `context.md`, `response.md`, `status.json`).
- `hw3/`: FastAPI + SQLAlchemy + Alembic layout, Kubernetes manifests, and the CI workflow that tests against Postgres and deploys to kind.

## Domain model

**Job**: `id`, `name` (1–100 printable characters), `spec` (workload parameters, see below), `state`, `max_attempts` (1–10, default 3), `attempt_count`, `created_at`, `started_at`, `finished_at`, `last_error`, `trace_context`.

**Attempt**: `id`, `job_id`, `number`, `worker_id`, `state`, `lease_expires_at`, `started_at`, `finished_at`, `exit_reason`, `failure_cause` (`workload` | `infra`, set on failure).

**LogLine**: `job_id`, `attempt_number`, `seq` (per job, monotonically increasing), `ts`, `line`. Capped at 1,000 lines per attempt.

`Attempt.number` increases over the job's whole lifetime and is never reset. `attempt_count` counts attempts since the job was submitted or last requeued, and the retry limit applies to it. The UI shows every attempt.

### Workload spec

```json
{
  "duration_s": 30,
  "fail_probability": 0.0,
  "mode": "normal"
}
```

- `duration_s`: 1–600. The worker sleeps in 1 s steps, emitting a log line and a heartbeat every step.
- `fail_probability`: 0.0–1.0. At the end, the attempt fails with this probability.
- `mode`: `normal` | `always_fail`. `always_fail` fails every attempt at the end of its duration.

### Failure causes

Every failed attempt carries a `failure_cause`:

- `workload`: the failure the user asked for, from `always_fail` or the `fail_probability` roll.
- `infra`: anything else, including an exception in the worker, a lost lease, or the `WORKER_FAULT` fault-injection setting.

User-impact alerts count only `infra` failures, so users testing failure handling never page the agent.

### Fault injection

The worker reads `WORKER_FAULT` at startup. With `WORKER_FAULT=fail_all`, every attempt fails after 1 s with cause `infra` and reason `injected fault: fail_all`. Any other value, or unset, means no fault.

### Job states

```
QUEUED ──claim──▶ RUNNING ──success──▶ SUCCEEDED
  ▲                 │ │
  │   failure and   │ └──failure, no attempts left──▶ FAILED
  └─attempts left───┘
                    │
                    └──lease expired──▶ STALLED ──requeue──▶ QUEUED

QUEUED / RUNNING / STALLED ──cancel──▶ CANCELLED
FAILED / STALLED ──requeue──▶ QUEUED   (resets attempt_count)
```

- Retries are immediate: a failed attempt with attempts left puts the job straight back in `QUEUED`.
- A worker claim sets a lease of 15 s. Each heartbeat extends it by 15 s.
- The sweeper runs in the API every 5 s and moves `RUNNING` jobs with an expired lease to `STALLED`, marking the attempt failed with cause `infra`. It does not requeue them. The sweep is a single idempotent `UPDATE … WHERE lease_expires_at < now()`, so any number of API replicas can run it.
- Terminal states: `SUCCEEDED`, `FAILED`, `CANCELLED`.
- Heartbeats and results for an attempt that is no longer the job's current `RUNNING` attempt (stalled, cancelled, or superseded) are rejected with `409`.

### Worker protocol

- When idle, the worker polls `claim` every 1 s.
- The claim response includes the job's spec, the attempt ID, and the job's stored trace context.
- On a `409` from heartbeat, the worker stops the attempt within 1 s and does not report a result. This is how cancellation reaches a running job.
- If the API is unreachable, the worker keeps running the workload and retries the heartbeat every 1 s. After 15 s without a successful heartbeat it abandons the attempt, since the lease has expired.
- A result report is retried every 1 s for up to 30 s, until it gets a success or a `409`.

## API

All public endpoints are under `/api/v1`, JSON in and out. Errors use `{"error": {"code": "...", "message": "..."}}`. Request validation errors return `400` through a custom exception handler.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/jobs` | Submit a job (`name`, `spec`, optional `max_attempts`). Returns `201` with the job. |
| `GET` | `/jobs?state=&limit=&cursor=` | List jobs, newest first. `limit` 1–100, default 50; the response carries `next_cursor`. |
| `GET` | `/jobs/{id}` | Job with its attempts. |
| `GET` | `/jobs/{id}/logs?after=` | Log lines with `seq > after`, up to 500 per page. |
| `POST` | `/jobs/{id}/requeue` | Requeue a `FAILED` or `STALLED` job. `409` otherwise. |
| `POST` | `/jobs/{id}/cancel` | Cancel a non-terminal job. `409` if already terminal. |
| `GET` | `/stats` | Counts by state, oldest queued age, job failure rate and `infra` attempt failure rate over the last 10 min. |

Internal endpoints are under `/internal/v1` and are reachable only inside the cluster or Compose network:

| Method | Path | Caller | Purpose |
| --- | --- | --- | --- |
| `POST` | `/worker/claim` | worker | Atomically claim the oldest `QUEUED` job; `204` if none. |
| `POST` | `/worker/attempts/{id}/heartbeat` | worker | Extend the lease; append log lines. `409` if the attempt is not current. |
| `POST` | `/worker/attempts/{id}/result` | worker | Report `succeeded`, or `failed` with a reason and cause. `409` if the attempt is not current. |
| `POST` | `/jobs/requeue-stalled` | agent | Requeue every `STALLED` job; returns the count. |
| `POST` | `/jobs/requeue-failed` | agent | Body `{since, until}`. Requeue every `FAILED` job finished in that window whose last attempt failed with cause `infra`; returns the count. |

## Frontend

- **Jobs page**: table of jobs with state badge, name, attempts, age, duration; filter by state; auto-refresh every 3 s. Summary bar from `/stats`.
- **Submit form**: name, duration, fail probability, mode, max attempts, with client-side validation matching the API.
- **Job detail**: attempts timeline with failure cause, logs that refresh every 2 s while the job is running, requeue/cancel buttons shown only when the state allows it.

## Observability

The key user journey is **submit → start → finish**.

Traces:

- `POST /jobs` starts the submit trace. The job stores its trace context.
- Each attempt is its own trace, started by the worker when the claim returns, with a span link to the submit span. It contains `run` and `result` spans, and the heartbeat and result requests propagate its context to the API.
- Spans carry `job.id` and `attempt.number`.

Logs are structured JSON and carry `trace_id`, `span_id`, and `job_id`.

Metrics:

| Metric | Type | Labels | Emitted by |
| --- | --- | --- | --- |
| `runwatch_jobs_submitted_total` | counter | — | API, on submit |
| `runwatch_jobs_finished_total` | counter | `outcome` (succeeded, failed, cancelled) | API, on the terminal transition |
| `runwatch_attempts_finished_total` | counter | `outcome` (succeeded, failed), `cause` (workload, infra, none) | API, on result or sweep |
| `runwatch_queue_depth` | gauge | — | API, read from the DB |
| `runwatch_oldest_queued_age_seconds` | gauge | — | API, read from the DB |
| `runwatch_jobs_stalled` | gauge | — | API, read from the DB |
| `runwatch_queue_wait_seconds` | histogram | — | API, on claim |
| `runwatch_job_run_seconds` | histogram | `outcome` | API, on result |
| HTTP server metrics | OTel defaults | route, status | API |

All job metrics come from the API, so they stay correct when workers are scaled to 0. Every API replica exports the DB-derived gauges, so alert rules and panels aggregate them with `max()`. Metrics are exported every 10 s.

Telemetry identifies jobs only by `job_id`. It never contains job names, job log content, or any secret (API keys, DB URLs, tokens). Grafana ships a provisioned "Runwatch" dashboard with the panels above and links from logs to traces.

## Alerts

Each alert describes something the user experiences.

| Alert | Condition (PromQL) | For | Meaning for the user |
| --- | --- | --- | --- |
| `QueueWaitHigh` | `max by (env) (runwatch_oldest_queued_age_seconds) > 120` | 1 m | "My job is not starting." |
| `JobsStalled` | `max by (env) (runwatch_jobs_stalled) > 0` | 1 m | "My job silently died." |
| `JobFailureRateHigh` | `sum by (env) (increase(runwatch_attempts_finished_total{outcome="failed",cause="infra"}[5m])) / sum by (env) (increase(runwatch_attempts_finished_total[5m])) > 0.5 and sum by (env) (increase(runwatch_attempts_finished_total[5m])) >= 5` | 1 m | "My jobs started failing." |
| `RunwatchApiDown` | `absent_over_time(runwatch_queue_depth{env="<env>"}[2m])`, one rule per environment | 1 m | "The app is down." This also covers the other alerts, which cannot fire without the API's metrics. |

Alertmanager routes each alert by its `env` label to that environment's responder agent webhook with a bearer token, and optionally to a chat channel. Rule evaluation interval is 15 s. Under Compose, `env` is `local`.

`JobsStalled` stays firing until stalled jobs are requeued or cancelled, so a stalled job left alone keeps paging. This is intended: a stalled job is user impact until someone acts on it.

## Responder agent

The agent is a small service that receives Alertmanager webhooks and runs a Claude tool-use loop with a fixed, allow-listed toolset. The model is `claude-opus-5-5`, configurable with `AGENT_MODEL`.

### Tools

Read:

- `get_stats()`
- `list_jobs(state)`
- `get_job_logs(id)`
- `k8s_get_pods(app)`
- `k8s_get_events()`
- `k8s_rollout_history(deployment)`: revisions with ReplicaSet creation time, image tag, and change-cause.
- `query_loki(query)`

Write:

- `k8s_rollout_restart(deployment)`, with `deployment` in `{worker, api}`.
- `k8s_scale(deployment, replicas)`, with `deployment` = `worker` and `1 ≤ replicas ≤ 3`.
- `k8s_rollout_undo(deployment)`, with `deployment` in `{worker, api}`.
- `requeue_stalled_jobs()`
- `requeue_failed_jobs(since, until)`: only `infra`-caused failures (see the internal API).

All k8s tools act on the agent's own namespace, set by configuration and never by the model. The agent's ServiceAccount has a namespaced Role with read access to pods, events, deployments, and replicasets, and `patch` on deployments and `deployments/scale`. It cannot run arbitrary shell or `kubectl`, delete resources, or touch the database directly.

### Untrusted input

Job names, job logs, and Loki results are user-controlled text. The agent wraps every tool result in a delimited block that the system prompt marks as data. The argument checks above are enforced in code, so a manipulated model still cannot act outside them.

### Run control

- One run per alert fingerprint at a time. Webhooks for a fingerprint with a run in progress, or within a 10-minute cooldown after a run ends, are recorded in that incident and start nothing new.
- A run is limited to 20 tool calls, 3 write actions, and 200k tokens.
- After acting, the agent re-checks the alert condition through `get_stats` every 30 s for up to 10 minutes and records whether the fix worked. A resolved notification from Alertmanager is also recorded.
- When the budget runs out, the condition is still true after verification, or the Anthropic API fails 3 times in a row, the incident ends with outcome `escalated` and is posted to the chat channel if one is configured.
- Without `ANTHROPIC_API_KEY`, the agent starts, logs a warning, and records incoming alerts with outcome `skipped_no_model`. Without Kubernetes credentials (for example, under Compose), the k8s tools are disabled and the agent can diagnose and requeue but cannot change deployments.

### Incident reports

Each incident produces a Markdown report (alert, evidence gathered, actions taken, outcome) in the agent's `incidents/` volume, viewable at the agent's `GET /incidents`. When `GITHUB_TOKEN` is configured, the report is also opened as a GitHub issue. Reports from the recorded drills are copied into the repository's `incidents/` directory as evidence.

After a `k8s_rollout_undo`, the report states that the cluster now differs from `main` and that `main` needs a revert PR before the next deploy, since CD would otherwise roll the bad release out again.

### Demo incidents

Both drills run in the `runwatch-dev` namespace of the kind cluster with the load generator running, and are screen-recorded for the demo.

1. **Worker down.** `make break-worker` scales the worker deployment to 0. Running jobs go `STALLED` within 25 s and queued jobs age past 2 minutes. Expected agent response: see 0 worker pods, scale the worker to 1, call `requeue_stalled_jobs`, confirm the queue drains.
2. **Bad release.** `make bad-release` runs `kubectl set env deployment/worker WORKER_FAULT=fail_all` with a change-cause annotation, creating a new rollout revision. The `infra` failure rate spikes. Expected agent response: match the start of the failures to the newest revision's creation time, `k8s_rollout_undo` the worker, call `requeue_failed_jobs` from that time until now. This also exercises the rollback path.

## Environments, deployment, and CI/CD

- `docker compose up --build` starts the API, worker, load generator, frontend, Postgres, the observability stack, and the agent.
- Everything runs at no cost on the development VM (4 vCPU, 5.3 GB RAM, 14 GB free disk). The VM is not reachable from the internet, so the deployment is shown through a recorded demo linked from the README.
- One kind cluster, `runwatch`, runs on the VM. It has three namespaces:
  - `runwatch-dev` and `runwatch-prod`, each with the app, load generator, an OTel Collector, and an agent. The agent's Role is limited to its namespace.
  - `runwatch-observability`, with one shared Prometheus, Tempo, Loki, Grafana, and Alertmanager.
- Each environment's collector sets the resource attribute `deployment.environment`, which Prometheus promotes to the label `env`. Loki and Tempo keep it as an attribute, and Grafana has an `env` dashboard variable.
- The ingress routes the frontend and `/api/v1` only, on `dev.localhost` and `prod.localhost`.
- Every pod has memory requests and limits so the whole cluster fits in 3 GB. Compose and the kind cluster are not meant to run at the same time.
- Kubernetes manifests: Kustomize base + `dev` and `prod` overlays.
- Images are pushed to GitHub Container Registry (free for public repositories), tagged with the commit SHA.
- GitHub Actions:
  - On PR: lint and run unit, frontend, and integration tests against Postgres on GitHub-hosted runners.
  - On merge to `main`: build and push images on GitHub-hosted runners, then deploy to **dev** automatically.
  - Deploy to **prod** with a manual approval (GitHub environment with a required reviewer).
- Deploy jobs run on a self-hosted GitHub Actions runner on the VM, which has access to the kind cluster. The runner only picks up deploy jobs triggered by a push to `main` or a manual dispatch, and the repository requires approval before workflows from fork PRs run, so code from outside contributors never runs on the VM.
- Rollback: `make rollback ENV=<env>` runs `kubectl rollout undo`, and redeploying a previous SHA tag through the deploy workflow is the second path. Both are documented in `docs/runbooks/rollback.md`.

## Development agent team

The project is built by coding agents working from the public tracker (GitHub Issues). `AGENTS.md` (imported by `CLAUDE.md`) describes the project and points to `_docs/process.md`, which defines the workflow, and to the role definitions in `_docs/team/`. Each role is also a Claude Code subagent in `.claude/agents/`.

| Role | Responsibility |
| --- | --- |
| Orchestrator | The main session. Schedules independent issues in waves of up to five, gives each its own git worktree and database, sends finished work to QA, and merges approved PRs into `main` one at a time. Never implements. |
| PM | Grooms an issue before implementation: goal, AC references, checkable acceptance criteria, out of scope, constraints, and dependencies. |
| Software engineer | Implements one groomed issue on a branch `issue-<n>-<slug>` in its worktree, with tests, and opens a PR with `Closes #<n>`. |
| QA engineer | A fresh agent that checks each PR against its acceptance criteria, runs `make test`, and posts PASS or FAIL on the PR. Changes no code. |

The human owner sets priorities, answers open questions, and can stop or redirect the orchestrator. Each issue and PR records which agent worked on it and what it did.

## Acceptance criteria

### Jobs and lifecycle

- **AC-1** Submitting a valid job returns `201` and the job appears in `GET /jobs` with state `QUEUED`.
- **AC-2** Invalid input (`duration_s` outside 1–600, `fail_probability` outside 0–1, unknown `mode`, `max_attempts` outside 1–10, `name` empty or over 100 characters) returns `400` with the error envelope and creates nothing.
- **AC-3** With one idle worker running, a `normal` job with `fail_probability: 0` reaches `SUCCEEDED` within `duration_s + 10` s.
- **AC-4** With 2 workers and 20 queued `normal` jobs with `fail_probability: 0`, every job ends `SUCCEEDED` with exactly one attempt, and no job ever has two attempts `RUNNING` at once.
- **AC-5** An `always_fail` job with `max_attempts: 3` ends `FAILED` with exactly 3 attempts, each with cause `workload`, and `last_error` set.
- **AC-6** Killing a worker mid-run moves its job to `STALLED` within 25 s; the job is not requeued automatically.
- **AC-7** `requeue` on a `STALLED` or `FAILED` job returns it to `QUEUED` with `attempt_count` 0, attempt numbers continue from the previous attempt, and the job can then succeed; on any other state it returns `409`.
- **AC-8** `cancel` on a `RUNNING` job sets `CANCELLED`; the worker's next heartbeat gets `409`, the worker stops the attempt within 2 s, and a later result for that attempt is rejected with `409`.
- **AC-9** Data survives restarting the API, the worker, and the database container. A job running during a database restart of under 10 s finishes `SUCCEEDED`.
- **AC-10** `GET /jobs` pagination returns every job exactly once across pages, and `GET /jobs/{id}/logs?after=` returns only lines with a higher `seq`.
- **AC-11** `GET /stats` matches the database for a seeded set of jobs, including oldest queued age and both failure rates.
- **AC-12** `requeue-failed` requeues only `FAILED` jobs in the given window whose last attempt failed with cause `infra`.

### Frontend

- **AC-13** The jobs page lists jobs with their state and updates without a manual reload.
- **AC-14** The submit form rejects invalid input before sending it and shows API errors when they occur.
- **AC-15** Job detail shows attempts with failure cause and logs; requeue/cancel buttons appear only in states where they are allowed and work end to end.

### Observability and alerting

- **AC-16** For one submitted job, Grafana shows the submit trace from `POST /jobs` and the attempt trace linked to it, and the job's log lines in Loki are reachable from the attempt trace by `trace_id`.
- **AC-17** All metrics in the table above are visible in Prometheus and on the provisioned dashboard.
- **AC-18** No job names, job log content, API keys, or connection strings appear in any span attribute, metric label, or telemetry log record (checked by a test that inspects exported spans, metrics, and logs).
- **AC-19** With the load generator running, demo incident 1 fires `JobsStalled` within 2 minutes and `QueueWaitHigh` within 4 minutes; demo incident 2 fires `JobFailureRateHigh` within 5 minutes.
- **AC-20** Ten `always_fail` jobs submitted alongside the load generator do not fire `JobFailureRateHigh`.

### Responder agent

- **AC-21** On the alerts from demo incident 1, the agent scales the worker back up and requeues stalled jobs without human input; the queue drains and the alerts resolve.
- **AC-22** On `JobFailureRateHigh` from demo incident 2, the agent rolls back the worker deployment and requeues the `infra`-failed jobs from the incident window; new jobs succeed again, the alert resolves, and the report flags the needed revert on `main`.
- **AC-23** Every agent run writes an incident report containing the alert, evidence, actions, and outcome.
- **AC-24** The agent enforces its tool rules in code (unit tested with fake model responses): a tool outside the list, a deployment outside the allowed set, `k8s_scale` outside 1–3, and any namespace argument are refused, and a run stops at its tool-call and write-action limits.
- **AC-25** A repeated webhook for an alert with a run in progress or in cooldown starts no new run and is recorded in the existing incident.

### Delivery

- **AC-26** `docker compose up --build` starts the full system with no other manual steps, with or without `ANTHROPIC_API_KEY`; the UI is reachable at `http://localhost:8080`.
- **AC-27** `make test` runs unit, frontend, and integration tests and they pass.
- **AC-28** A PR triggers CI tests; a merge to `main` deploys to dev; prod deploys only after manual approval; the rollback procedure is documented, and one exercised rollback is linked from the README (workflow run or incident report).
- **AC-29** The README links a recorded demo that shows a dev deploy from CI, a manual prod approval, the app running in both namespaces, and both incident drills.

## Test plan

- **Unit** (pytest): state machine transitions, attempt numbering, spec validation, failure cause assignment, lease/sweeper logic, stats calculation, agent tool rules, run control, and incident report formatting.
- **Frontend** (Vitest + React Testing Library): form validation, state badges, conditional action buttons, API error display.
- **Integration** (pytest against real Postgres in Compose/CI): API + worker end to end for AC-3 to AC-12, concurrent claim test for AC-4, telemetry export test for AC-18.
- **Incident drills** (scripted, run manually on k8s and recorded for the demo): AC-19 to AC-22.

## Task breakdown (seed for the public tracker)

Each task becomes a GitHub issue that links its AC IDs. Agents pick up issues, reference them in commits/PRs, and close them through the PR.

1. Repo skeleton, Makefile, lint, and the commands in `AGENTS.md`.
2. DB schema and Alembic migrations for jobs, attempts, log lines. *(AC-9)*
3. Job submission, listing, detail, logs, and stats API with validation. *(AC-1, AC-2, AC-10, AC-11)*
4. Worker claim/heartbeat/result protocol, simulated workload, fault injection. *(AC-3, AC-4, AC-5)*
5. Lease sweeper, `STALLED` state, requeue, cancel, `requeue-failed`. *(AC-6, AC-7, AC-8, AC-12)*
6. Load generator.
7. Frontend: jobs page, submit form, job detail. *(AC-13, AC-14, AC-15)*
8. Docker Compose for the app and Postgres. *(AC-26)*
9. Test suite and `make test`. *(AC-27)*
10. OTel instrumentation: traces across API and worker, metrics, structured logs. *(AC-16, AC-17, AC-18)*
11. Observability stack in Compose and k8s, Grafana dashboard, from `hw4/observability`. *(AC-17)*
12. Alert rules and Alertmanager webhook routing. *(AC-19, AC-20)*
13. Drill scripts: `make break-worker`, `make bad-release`. *(AC-19)*
14. Responder agent: webhook receiver, tool-use loop, tool rules, run control, incident reports. *(AC-21 to AC-25)*
15. Kubernetes manifests with dev/prod overlays, kind cluster setup (`make kind-up`), self-hosted runner setup. *(AC-28)*
16. CI/CD: tests on PR, auto-deploy dev, manual prod, rollback runbook. *(AC-28)*
17. README: problem, architecture, how to run, how to demo each incident, rubric mapping, demo recording. *(AC-29)*

## Cut list

If time runs short, these go first, in order:

1. GitHub issue creation for incident reports (files stay).
2. Cursor pagination on `GET /jobs` (a fixed `limit` stays).
3. The prod namespace and manual prod deploy. This drops CI/CD from 3 points to 2.
