# Runwatch

A small job run tracker: users submit simulated batch jobs and watch them
in a web UI, alerts fire on user impact, and a responder agent diagnoses
and fixes the two demo incidents. Project 1 of AI Dev Tools Zoomcamp
2026. Everything runs on one local VM at no cost (Compose, then one kind
cluster).

The spec is `_docs/SPEC.md`. Issues reference its acceptance criteria as
`AC-…`.

Stack: Python 3.12 with uv, FastAPI, SQLAlchemy, Alembic, PostgreSQL 16;
React + Vite frontend in `frontend/`; OpenTelemetry; Kustomize for k8s.

## Commands

The skeleton issue creates these; keep this list in sync with the
`Makefile`.

- `uv sync` - install dependencies
- `docker compose up --build` - run the full system, UI at
  http://localhost:8080, Grafana at http://localhost:3001, Prometheus at
  http://localhost:9091
- `docker compose stop` - stop it (never `down -v`)
- `cd frontend && npm ci` - install frontend dependencies
- `DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/runwatch_wt<n> make test` -
  run all tests: unit, then frontend, then integration. It needs
  `DATABASE_URL` and refuses to run without it
- `make test-unit` - unit tests only; needs no database
- `make test-integration` - integration tests; needs `DATABASE_URL`
- `DATABASE_URL=… uv run pytest tests/integration/test_e2e_jobs.py` - one
  integration file
- `make test-frontend` - frontend typecheck and tests
- `make test-alerts` - alert rules unit tests (`promtool`); needs the
  Prometheus container running (`docker compose up -d prometheus`)
- `make lint` - lint, same as the ruff command below
- `make kind-up` - create or start the kind cluster `runwatch` (stop
  Compose first; see `docs/runbooks/kind-cluster.md`)
- `make kind-down` - stop the cluster (never delete it)
- `make kind-load IMAGES="a:tag b:tag"` - load local images into the cluster
- `make k8s-render ENV=dev|prod`, `make k8s-validate`, `make k8s-secrets ENV=dev|prod` -
  render, validate (kubeconform in container `runwatch-kubeconform`) and
  create the Secrets for the k8s manifests in `k8s/`. All offline except
  `k8s-secrets` without `DRY_RUN=1`
- `uv run pytest tests/unit/test_package.py` - one test file
- `cd frontend && npx vitest run src/App.test.tsx` - one frontend test file
- `cd frontend && npm run dev` - dev server, proxies `/api/v1` to
  `API_PROXY_TARGET`
- `uv run ruff check . && uv run ruff format --check .` - lint
- `uv run alembic upgrade head` - apply migrations (reads `DATABASE_URL`)
- CI (`.github/workflows/ci.yaml`) runs `make lint`, `make test-unit`,
  `make test-integration` and `make test-frontend` on every PR, fails on
  any skipped test; the check to wait for is `CI passed`

DB-backed tests skip when `pytest` runs without `DATABASE_URL`, and
`make test` refuses to run without it. The integration tests empty the
`jobs`, `attempts` and `log_lines` tables in the database `DATABASE_URL`
names, so point it at your own database only.

## Rules

- Dependencies go in `pyproject.toml` (or `frontend/package.json`). Add
  one only when the issue allows it
- Settings come from environment variables, each listed in
  `.env.example`. Nothing hardcoded, no secrets in the repo
- Tests take their database from `DATABASE_URL` and bind free ports, so
  several worktrees can run them at once
- Telemetry identifies jobs by `job_id` only: no job names, log content
  or secrets

## Documents

- `_docs/process.md` - how work is organized: issues, roles, worktrees,
  parallel agents, PRs and merging. Read it before starting any issue
- `_docs/team/` - the PM, software engineer and QA roles
- `_docs/task-template.md` - the format of a groomed issue
- `docs/runbooks/` - operational runbooks (rollback)
