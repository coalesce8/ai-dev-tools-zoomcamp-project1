.PHONY: test test-frontend lint kind-up kind-down kind-load

KIND_KUBECONFIG ?= $(HOME)/.kube/runwatch-kind.config
KIND_MIN_AVAILABLE_MB ?= 1500
export KIND_KUBECONFIG KIND_MIN_AVAILABLE_MB

test:
	uv run pytest
	$(MAKE) test-frontend

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
