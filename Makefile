.PHONY: test test-unit test-frontend test-integration test-alerts need-database-url lint kind-up kind-down kind-load

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

test-alerts:
	@docker compose ps --status running --services 2>/dev/null | grep -qx prometheus || { echo "prometheus is not running: run 'docker compose up -d prometheus' first" >&2; exit 1; }
	docker compose exec -T prometheus promtool check config /etc/prometheus/prometheus.yaml
	docker compose exec -T prometheus sh -c 'promtool check rules /etc/prometheus/rules/*.yaml'
	docker compose exec -T prometheus promtool test rules /etc/prometheus/tests/alerts_test.yaml

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
