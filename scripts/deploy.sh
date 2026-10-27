#!/usr/bin/env bash
# Deploy the commit <sha>'s images to the kind cluster.
# Usage: scripts/deploy.sh <dev|prod> <40-hex sha>      (or: make deploy ENV=<env> SHA=<sha>)
#
# Environment:
#   KIND_KUBECONFIG  kubeconfig of the cluster (default ~/.kube/runwatch-kind.config)
#   KUBE_CONTEXT     context in it (default kind-runwatch)
#   IMAGE_REGISTRY   registry and owner of the images (default ghcr.io/coalesce8)
#   DRY_RUN=1        contact no cluster and no registry; print the plan, touch no tracked file
#   SKIP_PULL=1      skip `docker pull` (images built by hand and already tagged locally)
#   GITHUB_RUN_ID    recorded in the change-cause (default "manual")
#
# The scripts and manifests are those of the checked-out commit; only the image
# tags come from <sha>. The committed overlays keep `newTag: local`: the tag is
# replaced in a copy under .deploy/ (gitignored). A failed rollout does not roll
# back by itself: see docs/runbooks/rollback.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

KIND_KUBECONFIG="${KIND_KUBECONFIG:-$HOME/.kube/runwatch-kind.config}"
KUBE_CONTEXT="${KUBE_CONTEXT:-kind-runwatch}"
IMAGE_REGISTRY="${IMAGE_REGISTRY:-ghcr.io/coalesce8}"
NODE=runwatch-control-plane
CLUSTER=runwatch
DEPLOYMENTS=(api worker loadgen frontend otel-collector agent)
CEILING_S=900

env_name="${1:-}"
sha="${2:-}"
case "$env_name" in
  dev | prod) ;;
  *) echo "usage: $0 <dev|prod> <40-hex sha>: environment must be dev or prod" >&2; exit 2 ;;
esac
if ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "usage: $0 <dev|prod> <40-hex sha>: the sha must be 40 lowercase hex characters" >&2
  exit 2
fi

ns="runwatch-$env_name"
app_image="$IMAGE_REGISTRY/runwatch-app:$sha"
frontend_image="$IMAGE_REGISTRY/runwatch-frontend:$sha"
short="${sha:0:7}"
by="${GITHUB_RUN_ID:-manual}"
dry=0
[ "${DRY_RUN:-}" = "1" ] && dry=1
deadline=$((SECONDS + CEILING_S))

k() { kubectl --kubeconfig "$KIND_KUBECONFIG" --context "$KUBE_CONTEXT" "$@"; }
fail() { echo "deploy: $*" >&2; exit 1; }
step() { echo "== $*"; }
left() {
  local n=$((deadline - SECONDS))
  [ "$n" -gt 0 ] || fail "the overall ceiling of $((CEILING_S / 60)) minutes is used up"
  echo "$n"
}
min() { [ "$1" -lt "$2" ] && echo "$1" || echo "$2"; }

# The tag-replaced copy of k8s/. .deploy/ is overwritten, never deleted.
prepare_overlay() {
  mkdir -p .deploy/k8s
  cp -R k8s/. .deploy/k8s/
  overlay=".deploy/k8s/overlays/$env_name"
  sed -i -E "s/^( *newTag: ).*/\1$sha/" "$overlay/kustomization.yaml"
  [ "$(grep -c "newTag: $sha\$" "$overlay/kustomization.yaml")" = 2 ] \
    || fail "expected two newTag lines in $overlay/kustomization.yaml"
}

if [ "$dry" = 1 ]; then
  prepare_overlay
  echo "DRY_RUN: no cluster and no registry is contacted"
  echo "environment: $env_name (namespace $ns)"
  echo "images:"
  echo "  $app_image"
  echo "  $frontend_image"
  echo "newTag lines in $overlay/kustomization.yaml:"
  grep -E 'name: |newTag:' "$overlay/kustomization.yaml" | grep -B1 'newTag:' | sed 's/^/  /'
  echo "rendered image references:"
  kubectl kustomize "$overlay" | grep -E '^ *image: ' | sort -u | sed 's/^ */  /'
  echo "steps:"
  echo "  1. preconditions: node $NODE running, no Compose runwatch images running, Secrets postgres and agent in $ns, context $KUBE_CONTEXT"
  echo "  2. docker pull both images (unless SKIP_PULL=1), kind load docker-image both into cluster $CLUSTER"
  if [ "$env_name" = dev ]; then
    echo "  3. kubectl apply -k observability, then scripts/k8s-alertmanager-secret.sh (creates the Secret only when absent)"
  else
    echo "  3. (prod never applies observability/)"
  fi
  echo "  4. kubectl apply -k $overlay"
  echo "  5. kubectl annotate deployment/<name> kubernetes.io/change-cause=\"deploy $short by $by\" for ${DEPLOYMENTS[*]}"
  echo "  6. kubectl rollout status --timeout=300s for statefulset/postgres and each deployment (ceiling ${CEILING_S}s overall)"
  echo "  7. verify image tags end with :$sha, then smoke checks through $env_name.localhost (/ 200, /api/v1/stats 200, /internal/v1/ 404)"
  if [ "$env_name" = dev ]; then
    echo "alertmanager secret plan:"
    DRY_RUN=1 scripts/k8s-alertmanager-secret.sh | sed 's/^/  /'
  fi
  exit 0
fi

step "preconditions"
[ "$(docker inspect -f '{{.State.Running}}' "$NODE" 2>/dev/null || true)" = true ] \
  || fail "the kind node $NODE is not running: run 'make kind-up'"
for img in runwatch-app runwatch-frontend; do
  [ -z "$(docker ps -q --filter "ancestor=$img")" ] \
    || fail "a container from the local Compose image $img is running: run 'docker compose stop' (Compose and kind never run together)"
done
for secret in postgres agent; do
  k -n "$ns" get secret "$secret" > /dev/null 2>&1 \
    || fail "Secret $secret is missing in $ns: run 'make k8s-secrets ENV=$env_name'"
done
k config get-contexts -o name 2> /dev/null | grep -qx "$KUBE_CONTEXT" \
  || fail "context $KUBE_CONTEXT is not in $KIND_KUBECONFIG: run 'make kind-up'"

prepare_overlay

step "images: $app_image $frontend_image"
if [ "${SKIP_PULL:-}" != 1 ]; then
  timeout "$(min "$(left)" 600)" docker pull "$app_image" \
    || fail "docker pull $app_image failed (was this sha built and pushed?)"
  timeout "$(min "$(left)" 600)" docker pull "$frontend_image" \
    || fail "docker pull $frontend_image failed (was this sha built and pushed?)"
fi
timeout "$(min "$(left)" 600)" scripts/kind-cluster.sh load "$app_image" "$frontend_image"

if [ "$env_name" = dev ]; then
  step "observability"
  k apply -k observability
  scripts/k8s-alertmanager-secret.sh
fi

step "apply $overlay"
k apply -k "$overlay"

step "change-cause"
for d in "${DEPLOYMENTS[@]}"; do
  k -n "$ns" annotate "deployment/$d" "kubernetes.io/change-cause=deploy $short by $by" --overwrite
done

step "rollout status"
wait_for() {
  if ! k -n "$ns" rollout status "$1" "--timeout=$(min "$(left)" 300)s"; then
    k -n "$ns" get pods >&2 || true
    fail "$1 did not roll out in time; no automatic rollback (see docs/runbooks/rollback.md)"
  fi
}
wait_for statefulset/postgres
for d in "${DEPLOYMENTS[@]}"; do
  wait_for "deployment/$d"
done

step "verify images"
found=0
while read -r name images; do
  for img in $images; do
    case "$img" in
      "$IMAGE_REGISTRY"/runwatch-app:* | "$IMAGE_REGISTRY"/runwatch-frontend:*)
        found=$((found + 1))
        [[ "$img" == *":$sha" ]] || fail "check failed: deployment/$name runs $img, expected tag :$sha"
        ;;
    esac
  done
done < <(k -n "$ns" get deploy -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.template.spec.initContainers[*].image}{" "}{.spec.template.spec.containers[*].image}{"\n"}{end}')
[ "$found" -gt 0 ] || fail "check failed: no Runwatch image found in the Deployments of $ns"
echo "$found container image(s) end with :$sha"

step "smoke checks via $env_name.localhost"
host="$env_name.localhost"
smoke() {
  local path="$1" want="$2" code="" end=$((SECONDS + 60))
  while :; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 --resolve "$host:80:127.0.0.1" "http://$host$path" || true)
    [ "$code" = "$want" ] && { echo "GET $path -> $code"; return; }
    [ "$SECONDS" -lt "$end" ] || fail "check failed: GET $path returned $code, expected $want"
    sleep 3
  done
}
smoke / 200
smoke /api/v1/stats 200
smoke /internal/v1/ 404

step "deployed $short to $ns"
