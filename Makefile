.PHONY: test test-unit test-frontend test-integration need-database-url lint kind-up kind-down kind-load

KIND_KUBECONFIG ?= $(HOME)/.kube/runwatch-kind.config
KIND_MIN_AVAILABLE_MB ?= 1500
export KIND_KUBECONFIG KIND_MIN_AVAILABLE_MB

test:
	@test -n "$$DATABASE_URL" || { $(MAKE) --no-print-directory need-database-url; exit 1; }
	$(MAKE) test-unit
	$(MAKE) test-frontend
	$(MAKE) test-integration

test-unit:
	uv run pytest -rs tests/unit

test-integration:
	@test -n "$$DATABASE_URL" || { $(MAKE) --no-print-directory need-database-url; exit 1; }
	uv run pytest -rs tests/integration

need-database-url:
	@echo "DATABASE_URL is not set. The integration tests need a PostgreSQL database and empty its tables." >&2
	@echo "Run it as: DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/runwatch_wt<n> make test" >&2

test-frontend:
	@test -d frontend/node_modules || { echo "frontend/node_modules is missing: run 'cd frontend && npm ci' first" >&2; exit 1; }
	cd frontend && npm run typecheck && npm test

lint:
	uv run ruff check . && uv run ruff format --check .

kind-up:
	scripts/kind-cluster.sh up

kind-down:
	scripts/kind-cluster.sh down

kind-load:
	scripts/kind-cluster.sh load $(IMAGES)
