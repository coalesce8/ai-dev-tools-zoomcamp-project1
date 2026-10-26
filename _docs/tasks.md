# Runwatch backlog

Tasks derived from `_docs/SPEC.md`. Each task names the spec sections and acceptance criteria (`AC-…`) it covers; read those sections before starting. Paths are relative to the project root.

## 1. Project skeleton
Goal: A Python project where `make test` and ruff pass, and the commands in `AGENTS.md` are real.
Description: Create `pyproject.toml` (managed with `uv`, Python 3.12) with a `runwatch` package under `app/`, and pytest and ruff as dev dependencies. Add one trivial test under `tests/unit/` and a `Makefile` whose `test` target runs pytest; no application code yet. Update the Commands section of `AGENTS.md` to match. The agent team docs (`AGENTS.md`, `_docs/process.md`, `_docs/team/`, `.claude/agents/`) already exist.

## 2. Database models and initial migration
Goal: The `jobs`, `attempts`, and `log_lines` tables exist and are created by Alembic.
Description: Define SQLAlchemy models for Job, Attempt, and LogLine with the fields in the spec's "Domain model" section, using `hw3/` as the layout reference. Add Alembic configuration and an initial migration, with the indexes needed for claiming the oldest `QUEUED` job and paging jobs newest first. Add a test that runs the migration against Postgres and checks the tables.

## 3. Job state machine
Goal: All job and attempt transitions live in one pure, unit-tested module.
Description: Implement the transitions from the spec's "Job states" section (claim, success, failure with and without attempts left, lease expiry, requeue, cancel) as functions that take the current state and return the new state or raise a conflict error. Cover attempt numbering that never resets, `attempt_count` reset on requeue, and failure cause assignment (`workload` vs `infra`). No database or HTTP code in this module.

## 4. API skeleton and error handling
Goal: A FastAPI app that starts, connects to Postgres, and returns errors in the spec's envelope.
Description: Create the FastAPI app with settings from environment variables, a DB session dependency, and `/healthz` and `/readyz` (ready only when the DB is reachable). Add exception handlers so validation errors return `400` and all errors use `{"error": {"code", "message"}}`, as in the spec's "API" section. Mount empty routers for `/api/v1` and `/internal/v1`.

## 5. Job submission endpoint
Goal: `POST /api/v1/jobs` creates a validated job in state `QUEUED`.
Description: Add request and response schemas for a job and its workload spec with the limits from "Domain model" and "Workload spec" (name 1–100 printable characters, `duration_s` 1–600, `fail_probability` 0–1, `mode` in `normal`/`always_fail`, `max_attempts` 1–10 default 3). Return `201` with the job. Tests cover AC-1 and every invalid case in AC-2, including that nothing is stored on failure.

## 6. Job list, detail, and logs endpoints
Goal: Users can list jobs with cursor pagination, fetch one job with its attempts, and read its logs incrementally.
Description: Implement `GET /api/v1/jobs?state=&limit=&cursor=` (newest first, `limit` 1–100 default 50, `next_cursor` in the response) and `GET /api/v1/jobs/{id}` returning the job and all attempts. Use a stable cursor (for example `created_at` plus `id`) so jobs created during paging are neither skipped nor repeated. Add `GET /api/v1/jobs/{id}/logs?after=` returning log lines for a job with `seq > after`, ordered by `seq`, up to 500 per page, across all attempts and tagged with `attempt_number`. Tests cover AC-10, `404` for unknown IDs, an empty log result, and the log page size limit. Writing log lines belongs to the heartbeat endpoint task.

## 7. Worker claim endpoint
Goal: Workers can atomically claim the oldest queued job.
Description: Implement `POST /internal/v1/worker/claim` using `SELECT … FOR UPDATE SKIP LOCKED`: move the oldest `QUEUED` job to `RUNNING`, create the next Attempt with a 15 s lease, and return the spec, attempt ID, and stored trace context; return `204` when nothing is queued. See "Worker protocol" and "Job states" in the spec. Include a concurrency test where many parallel claims never hand the same job out twice (AC-4).

## 8. Heartbeat and result endpoints
Goal: Workers can extend leases, append logs, and report attempt outcomes.
Description: Implement `POST /internal/v1/worker/attempts/{id}/heartbeat` (extend lease by 15 s, append log lines with per-job `seq`, cap 1,000 lines per attempt) and `POST /internal/v1/worker/attempts/{id}/result` (succeeded, or failed with reason and cause; retry immediately if attempts remain, else `FAILED` with `last_error`). Both return `409` when the attempt is not the job's current `RUNNING` attempt. Tests cover AC-5 and the `409` cases at the API level.

## 9. Lease sweeper
Goal: Jobs whose worker disappeared move to `STALLED` automatically.
Description: Add a background task in the API that runs every 5 s and, in a single idempotent `UPDATE … WHERE lease_expires_at < now()`, moves `RUNNING` jobs to `STALLED` and marks their attempt failed with cause `infra`. It must be safe with several API replicas and must never requeue. See "Job states"; tests cover the sweep logic behind AC-6.

## 10. Requeue and cancel endpoints
Goal: Users can requeue failed or stalled jobs and cancel unfinished ones, and the responder agent can requeue in bulk.
Description: Implement `POST /api/v1/jobs/{id}/requeue` (`FAILED` or `STALLED` only, resets `attempt_count`, attempt numbers continue) and `POST /api/v1/jobs/{id}/cancel` (any non-terminal state), both returning `409` otherwise. Cancelling a `RUNNING` job must make that attempt non-current so its next heartbeat or result gets `409`. Also implement `POST /internal/v1/jobs/requeue-stalled` and `POST /internal/v1/jobs/requeue-failed` with body `{since, until}`, both returning the count requeued. `requeue-failed` must only touch `FAILED` jobs finished in the window whose last attempt failed with cause `infra`. Tests cover the API side of AC-7 and AC-8, and AC-12 with a mix of `workload` and `infra` failures inside and outside the window.

## 11. Stats endpoint
Goal: `GET /api/v1/stats` reports the queue's health from the database.
Description: Return counts by state, oldest queued age, job failure rate, and `infra` attempt failure rate over the last 10 minutes, as described in the "API" section. Define the rate formulas in code comments so they match what the alerts measure. Tests seed a known set of jobs and attempts and check every field (AC-11).

## 12. Worker process and load generator
Goal: A standalone worker claims jobs, runs the simulated workload, and reports results, and a load generator keeps user-impact metrics populated.
Description: Build the worker entry point following "Worker protocol", "Workload spec", and "Fault injection": poll claim every 1 s, sleep in 1 s steps with a log line and heartbeat each step, roll `fail_probability` at the end, and honour `WORKER_FAULT=fail_all`; handle `409` on heartbeat (stop within 1 s, no result), an unreachable API (abandon after 15 s), and result retries (every 1 s for up to 30 s), unit tested against a fake API client. Add a load generator process that submits one `normal` job with `duration_s: 10` and `fail_probability: 0` every `LOADGEN_INTERVAL_S` seconds (default 10) through the public API. It should log and keep going when the API is unavailable. A unit test with the same fake client checks its payload and interval.

## 13. Frontend skeleton
Goal: A React + Vite app that builds and has one passing Vitest test.
Description: Create the frontend under `frontend/` with React, Vite, Vitest, and React Testing Library, plus a typed API client for `/api/v1` that turns the error envelope into a displayable error. Add routing for a jobs page and a job detail page with placeholder content, and a `make test-frontend` target.

## 14. Jobs page
Goal: Users see all jobs and a summary that refresh on their own.
Description: Build the jobs page from the spec's "Frontend" section: a table with state badge, name, attempts, age, and duration, a state filter, a summary bar from `/stats`, and auto-refresh every 3 s. Component tests cover state badges, filtering, and API error display (AC-13).

## 15. Submit form
Goal: Users can submit a job from the UI with validation matching the API.
Description: Add a form with name, duration, fail probability, mode, and max attempts, validating the same limits as the API before sending. Show the API error message when the server rejects a request, and refresh the jobs list on success. Component tests cover each invalid input and a server-side error (AC-14).

## 16. Job detail page
Goal: Users can inspect one job's attempts and logs and act on it.
Description: Show the job, an attempts timeline with failure cause, and logs fetched incrementally with `after=` every 2 s while the job is running. Show requeue only for `FAILED`/`STALLED` and cancel only for non-terminal states, and refresh after either action. Component tests cover button visibility per state and log appending (AC-15).

## 17. Docker images and Compose for the app
Goal: `docker compose up --build` runs Postgres, API, worker, load generator, and frontend.
Description: Write Dockerfiles for the Python services (one image, different commands) and the frontend (served on port 8080 with `/api/v1` proxied to the API). Add `compose.yaml` with Postgres 16 on a named volume, a migration step before the API starts, health checks, and memory limits, keeping internal endpoints unreachable through the frontend proxy. Covers the app part of AC-26.

## 18. Integration test harness
Goal: Integration tests run the real API and worker against Postgres.
Description: Add a pytest setup under `tests/integration/` that starts Postgres (Compose locally, a service container in CI), runs migrations, and starts the API and worker processes. Write end-to-end tests for AC-3, AC-5, AC-7, AC-8, and AC-12. Extend `make test` to run unit, frontend, and integration tests (AC-27).

## 19. Concurrency and worker-loss tests
Goal: The system behaves correctly with multiple workers and when a worker dies.
Description: Add integration tests for AC-4 (2 workers, 20 jobs, exactly one attempt each, never two `RUNNING` attempts at once) and AC-6 (kill a worker mid-run, job goes `STALLED` within 25 s and stays there). Fix any bugs these tests expose in the worker or API.

## 20. Restart resilience tests
Goal: Data and running jobs survive restarts of the API, worker, and database.
Description: Add integration tests for AC-9: data survives restarting the API, the worker, and the Postgres container, and a job running through a DB restart of under 10 s finishes `SUCCEEDED`. Fix any bugs these tests expose, such as missing DB reconnects in the API or heartbeat and result retries in the worker.

## 21. Tracing across API and worker
Goal: Each job's submit and attempts show up as linked traces.
Description: Instrument the API and worker with OpenTelemetry exporting over OTLP, per the "Observability" section: `POST /jobs` starts the submit trace and stores its context on the job, each attempt is a new trace with a span link to the submit span, `run` and `result` spans, and context propagated on heartbeat and result calls. Spans carry `job.id` and `attempt.number`. Covers the tracing part of AC-16.

## 22. Application metrics
Goal: All metrics in the spec's metrics table are exported from the API.
Description: Add the counters, gauges, and histograms listed under "Observability" with exactly the given names and labels, emitted by the API only. The DB-derived gauges (`queue_depth`, `oldest_queued_age_seconds`, `jobs_stalled`) are read from the database on each export; export interval is 10 s. Unit tests check that each event increments the right series (AC-17, metrics part).

## 23. Structured logs and telemetry privacy test
Goal: Logs are JSON with trace correlation, and no telemetry leaks sensitive data.
Description: Configure JSON logging in the API, worker, load generator, and agent with `trace_id`, `span_id`, and `job_id`, exported over OTLP. Add a test that runs a job with a distinctive name and log content plus fake secrets, captures exported spans, metrics, and logs in memory, and asserts none of them appear (AC-18).

## 24. Observability stack in Compose
Goal: Traces, metrics, and logs from the app are visible in Grafana locally.
Description: Port the collector, Prometheus, Tempo, Loki, and Grafana configuration from `hw4/observability/` into this project's Compose setup, with the collector setting `deployment.environment=local` and Prometheus promoting it to `env`. Provision a "Runwatch" dashboard with the spec's metric panels, an `env` variable, and log-to-trace links. Covers AC-16 and AC-17 locally.

## 25. Alert rules and Alertmanager routing
Goal: User-impact alerts fire and reach the responder agent's webhook.
Description: Add the four alert rules from the "Alerts" section with a 15 s evaluation interval, and Alertmanager config that routes by `env` to that environment's agent webhook with a bearer token. Add `promtool test rules` cases showing each alert fires and that ten `always_fail` jobs do not fire `JobFailureRateHigh` (AC-20). Wire both into Compose.

## 26. Responder agent service and run control
Goal: The agent receives Alertmanager webhooks and decides whether to start a run.
Description: Create the agent service with a bearer-token-protected webhook endpoint, per-fingerprint run tracking (one run at a time, 10-minute cooldown), and an incident directory per run following the layout in `hw4/incident-response/`. Repeated webhooks during a run or cooldown are appended to the existing incident (AC-25). Without `ANTHROPIC_API_KEY`, alerts are recorded with outcome `skipped_no_model`.

## 27. Agent tools and tool rules
Goal: The agent's allow-listed tools exist and their limits are enforced in code.
Description: Implement the read and write tools from the "Responder agent → Tools" section against the internal API, Loki, and the Kubernetes API, with the namespace fixed by configuration. Enforce the argument rules in code: unknown tools, deployments outside the allowed sets, `k8s_scale` outside 1–3, and any namespace argument are refused, and the k8s tools are disabled when no Kubernetes credentials are present. Unit tests cover every refusal (AC-24).

## 28. Agent tool-use loop
Goal: The agent diagnoses an alert with Claude and applies fixes within its budget.
Description: Implement the Claude tool-use loop (model `claude-opus-5-5`, overridable by `AGENT_MODEL`) with a system prompt that marks tool results as data, every result wrapped in a delimited block, and limits of 20 tool calls, 3 write actions, and 200k tokens. After acting, re-check via `get_stats` every 30 s for up to 10 minutes; end with `escalated` on budget exhaustion, failed verification, or 3 consecutive API failures. Unit tests use fake model responses to cover the limits and escalation paths (AC-24).

## 29. Incident reports
Goal: Every agent run leaves a readable report of what happened.
Description: Generate a Markdown report per incident with the alert, evidence gathered, actions taken, and outcome, and serve the list at the agent's `GET /incidents`. After a `k8s_rollout_undo`, the report must state that the cluster differs from `main` and a revert PR is needed. When `GITHUB_TOKEN` is set, also open the report as a GitHub issue (lowest priority on the cut list). Covers AC-23.

## 30. Agent in Compose
Goal: The full system, including the agent, starts with one Compose command.
Description: Add the agent to `compose.yaml` with its incidents volume, Alertmanager pointing at it, and k8s tools disabled. Verify `docker compose up --build` works with and without `ANTHROPIC_API_KEY` and the UI is at `http://localhost:8080` (AC-26). Document required and optional environment variables in `.env.example`.

## 31. Kubernetes manifests for the app
Goal: The app runs in Kubernetes from a Kustomize base with dev and prod overlays.
Description: Write a Kustomize base for Postgres, API, worker, load generator, frontend, collector, and agent, plus `dev` and `prod` overlays for the `runwatch-dev` and `runwatch-prod` namespaces, starting from `hw3/` manifests. Every pod has memory requests and limits, and images are referenced by commit SHA tag. Include the agent's ServiceAccount and namespaced Role exactly as described under "Responder agent → Tools".

## 32. Kubernetes observability namespace
Goal: One shared observability stack in `runwatch-observability` serves both environments.
Description: Add manifests for Prometheus, Tempo, Loki, Grafana, and Alertmanager in `runwatch-observability`, reusing the configs from task 24 and the rules and routing from task 25. Each environment's collector sets `deployment.environment` to `dev` or `prod`, and Alertmanager routes to that environment's agent. Keep the whole cluster within 3 GB of memory.

## 33. kind cluster setup
Goal: `make kind-up` creates a working local cluster for the demo.
Description: Add a kind config and `make kind-up` / `make kind-down` that create the `runwatch` cluster, install an ingress controller, and route the frontend and `/api/v1` on `dev.localhost` and `prod.localhost`. Internal endpoints must not be routed. Document resource expectations for the VM (4 vCPU, 5.3 GB RAM) and that Compose should be stopped first.

## 34. CI workflow for pull requests
Goal: Every PR runs lint and the full test suite on GitHub-hosted runners.
Description: Add a GitHub Actions workflow triggered on pull requests that lints, then runs unit, frontend, and integration tests with a Postgres service container, reusing patterns from the `hw3/` CI workflow. It must not use the self-hosted runner. Covers the PR part of AC-28.

## 35. CD to dev and prod
Goal: Merges to `main` deploy to dev automatically; prod deploys after manual approval.
Description: On push to `main`, build and push images to GHCR tagged with the commit SHA on GitHub-hosted runners, then deploy the dev overlay on the self-hosted runner. Add a prod deploy job gated by a GitHub environment with a required reviewer. Document self-hosted runner setup and the fork-PR approval setting so outside code never runs on the VM (AC-28).

## 36. Rollback path, runbook, and drill scripts
Goal: A bad deploy can be rolled back two documented ways, and both demo incidents can be triggered with one command each.
Description: Add `make rollback ENV=<env>` (runs `kubectl rollout undo` in that namespace) and support redeploying a previous SHA through a manual dispatch of the deploy workflow. Write `docs/runbooks/rollback.md` covering both. Exercise one rollback and keep the link to the workflow run or incident report for the README (AC-28). Also add `make break-worker` (scale the worker in `runwatch-dev` to 0) and `make bad-release` (`kubectl set env deployment/worker WORKER_FAULT=fail_all` with a change-cause annotation) as described under "Demo incidents", plus a `make heal` that restores a clean state. Each script prints what it did and which alerts to expect.

## 37. Run and record the incident drills
Goal: Both drills are shown working end to end on kind, with evidence in the repo.
Description: With the load generator running in `runwatch-dev`, run both drills and check alert timings (AC-19) and the agent's fixes (AC-21, AC-22). Fix any gaps found, copy the resulting incident reports into `incidents/`, and screen-record both drills for the demo.

## 38. README and demo recording
Goal: A reviewer can understand, run, and evaluate the project from the README alone.
Description: Write the README with the problem, an architecture diagram, how to run locally and on kind, how to trigger each incident, and a mapping from the course rubric to files and evidence. Record and link a demo showing a dev deploy from CI, a manual prod approval, the app in both namespaces, and both drills (AC-29).
