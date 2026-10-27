#!/usr/bin/env bash
# Manage the local kind cluster "runwatch". Called by `make kind-up`,
# `make kind-down` and `make kind-load`. Nothing here deletes anything:
# down is `docker stop`, up is `docker start` or `kind create`.
set -euo pipefail

CLUSTER=runwatch
CONTEXT=kind-runwatch
NODE=runwatch-control-plane
KIND_KUBECONFIG="${KIND_KUBECONFIG:-$HOME/.kube/runwatch-kind.config}"
KIND_MIN_AVAILABLE_MB="${KIND_MIN_AVAILABLE_MB:-1500}"
KIND_MEMORY="${KIND_MEMORY:-3g}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Every kubectl call names the dedicated kubeconfig and the context.
kc() { kubectl --kubeconfig "$KIND_KUBECONFIG" --context "$CONTEXT" "$@"; }

node_state() {
  local state
  state=$(docker ps -a --filter "name=^${NODE}\$" --format '{{.State}}')
  echo "${state:-absent}"
}

check_memory() {
  local avail
  avail=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
  if [ "$avail" -lt "$KIND_MIN_AVAILABLE_MB" ]; then
    echo "refusing to start: MemAvailable is ${avail} MB, need at least ${KIND_MIN_AVAILABLE_MB} MB (KIND_MIN_AVAILABLE_MB)" >&2
    exit 1
  fi
  echo "MemAvailable ${avail} MB (minimum ${KIND_MIN_AVAILABLE_MB} MB)"
}

check_compose_stopped() {
  local running
  running=$(docker ps --filter label=com.docker.compose.project=runwatch --format '{{.Names}}')
  if [ -n "$running" ]; then
    echo "Runwatch Compose services are running; stop them first (docker compose stop, never down):" >&2
    echo "$running" >&2
    exit 1
  fi
}

check_port_80() {
  if [ -n "$(ss -ltnH 'sport = :80')" ]; then
    echo "something already listens on port 80; stop it first, this script does not pick another port" >&2
    ss -ltn 'sport = :80' >&2
    exit 1
  fi
}

cap_memory() {
  docker update --memory "$KIND_MEMORY" --memory-swap "$KIND_MEMORY" "$NODE" >/dev/null
}

# After a restart the API server answers before RBAC is back, so retry with a ceiling.
wait_for_node() {
  local i
  for i in $(seq 1 60); do
    if kc wait --for=condition=Ready node --all --timeout=5s >/dev/null 2>&1; then
      echo "node Ready"
      return 0
    fi
    sleep 2
  done
  echo "node not Ready within 120s" >&2
  kc get nodes >&2 || true
  exit 1
}

up() {
  local state
  state=$(node_state)
  mkdir -p "$(dirname "$KIND_KUBECONFIG")"
  case "$state" in
    running)
      echo "cluster $CLUSTER is already running"
      ;;
    absent)
      check_compose_stopped
      check_memory
      check_port_80
      kind create cluster --name "$CLUSTER" --config "$ROOT/deploy/kind/kind-config.yaml" \
        --kubeconfig "$KIND_KUBECONFIG" --wait 120s
      ;;
    *)
      check_compose_stopped
      check_memory
      check_port_80
      docker start "$NODE" >/dev/null
      ;;
  esac
  cap_memory
  # The API server port is random and can change after a restart.
  kind export kubeconfig --name "$CLUSTER" --kubeconfig "$KIND_KUBECONFIG"
  wait_for_node
  kc apply -k "$ROOT/deploy/kind"
  if ! kc wait --for=condition=ready pod -l app.kubernetes.io/component=controller \
    -n ingress-nginx --timeout=180s; then
    echo "ingress-nginx controller did not become ready within 180s" >&2
    kc get pods -n ingress-nginx >&2 || true
    exit 1
  fi
  echo "cluster $CLUSTER is up (kubeconfig $KIND_KUBECONFIG, context $CONTEXT)"
}

down() {
  if [ "$(node_state)" = absent ]; then
    echo "no $NODE container; nothing to stop"
    return 0
  fi
  docker stop "$NODE" >/dev/null
  echo "stopped $NODE (not deleted)"
}

load() {
  if [ "$#" -eq 0 ]; then
    echo 'usage: make kind-load IMAGES="image:tag [image:tag ...]"' >&2
    exit 2
  fi
  if [ "$(node_state)" != running ]; then
    echo "cluster $CLUSTER is not running: run 'make kind-up' first" >&2
    exit 1
  fi
  if kind load docker-image --name "$CLUSTER" "$@"; then
    return 0
  fi
  # With Docker's containerd image store, multi-platform images (hello-world,
  # most registry images) fail to import whole. Retry with the node's platform only.
  local arch img
  arch=$(docker info -f '{{.Architecture}}')
  case "$arch" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; esac
  echo "retrying with a single-platform archive (linux/$arch)" >&2
  for img in "$@"; do
    docker save --platform "linux/$arch" "$img" | kind load image-archive --name "$CLUSTER" /dev/stdin
  done
}

cmd="${1:-}"
[ "$#" -gt 0 ] && shift
case "$cmd" in
  up) up ;;
  down) down ;;
  load) load "$@" ;;
  *) echo "usage: $0 up|down|load [images...]" >&2; exit 2 ;;
esac
