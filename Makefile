.PHONY: test test-frontend lint

test:
	uv run pytest
	$(MAKE) test-frontend

test-frontend:
	@test -d frontend/node_modules || { echo "frontend/node_modules is missing: run 'cd frontend && npm ci' first" >&2; exit 1; }
	cd frontend && npm run typecheck && npm test

lint:
	uv run ruff check . && uv run ruff format --check .
