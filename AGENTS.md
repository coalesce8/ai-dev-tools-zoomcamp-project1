# Runwatch

A small job run tracker: users submit simulated batch jobs and watch them
in a web UI, alerts fire on user impact, and a responder agent diagnoses
and fixes the two demo incidents. Final project for AI Dev Tools Zoomcamp
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
- `make test` - unit, frontend and integration tests
- `uv run pytest tests/unit/test_x.py` - one test file
- `uv run ruff check . && uv run ruff format --check .` - lint
- `uv run alembic upgrade head` - apply migrations

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
