.PHONY: test test-unit test-frontend test-integration test-alerts need-database-url lint kind-up kind-down kind-load k8s-render k8s-validate k8s-secrets rollback break-worker bad-release heal

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

# Offline: renders the manifests; no cluster is contacted.
k8s-render:
	@case "$(ENV)" in dev|prod) ;; *) echo "ENV must be dev or prod, e.g. make k8s-render ENV=dev" >&2; exit 1;; esac
	@command -v kubectl >/dev/null || { echo "kubectl is not installed" >&2; exit 1; }
	kubectl kustomize k8s/overlays/$(ENV)

# Offline: renders both overlays and checks them with kubeconform in one named, reusable container.
k8s-validate:
	@command -v kubectl >/dev/null || { echo "kubectl is not installed" >&2; exit 1; }
	@command -v docker >/dev/null || { echo "docker is not installed" >&2; exit 1; }
	@docker container inspect runwatch-kubeconform >/dev/null 2>&1 || \
		docker create -i --name runwatch-kubeconform ghcr.io/yannh/kubeconform:v0.7.0 \
		-strict -summary -kubernetes-version 1.34.0 - >/dev/null
	@set -e; for env in dev prod; do \
		echo "== $$env"; \
		kubectl kustomize k8s/overlays/$$env > "$${TMPDIR:-/tmp}/runwatch-k8s-$$env.yaml"; \
		echo "resources: $$(grep -c '^kind:' "$${TMPDIR:-/tmp}/runwatch-k8s-$$env.yaml")"; \
		docker start -a -i runwatch-kubeconform < "$${TMPDIR:-/tmp}/runwatch-k8s-$$env.yaml"; \
	done

# Talks to the kind cluster unless DRY_RUN=1.
k8s-secrets:
	scripts/k8s-secrets.sh $(ENV)

# Rollback and drills act on the kind cluster; DRY_RUN=1 prints instead of running.
rollback:
	scripts/drills/rollback.sh

break-worker:
	scripts/drills/break-worker.sh

bad-release:
	scripts/drills/bad-release.sh

heal:
	scripts/drills/heal.sh
