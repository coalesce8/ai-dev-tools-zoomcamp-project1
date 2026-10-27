#!/usr/bin/env bash
# Create Secret alertmanager-webhook (keys dev-token, prod-token) in
# runwatch-observability from the webhook-token of Secret agent in runwatch-dev
# and runwatch-prod. Created only when absent; no value is printed or written
# to a file. Called by scripts/deploy.sh dev.
# DRY_RUN=1 prints the kubectl command with values <redacted> and runs nothing.
# Rotation is a manual step: docs/runbooks/deploy.md.
set -euo pipefail

KIND_KUBECONFIG="${KIND_KUBECONFIG:-$HOME/.kube/runwatch-kind.config}"
KUBE_CONTEXT="${KUBE_CONTEXT:-kind-runwatch}"
ns=runwatch-observability

k() { kubectl --kubeconfig "$KIND_KUBECONFIG" --context "$KUBE_CONTEXT" "$@"; }

if [ "${DRY_RUN:-}" = "1" ]; then
  echo "kubectl --kubeconfig $KIND_KUBECONFIG --context $KUBE_CONTEXT -n $ns create secret generic alertmanager-webhook --from-literal=dev-token=<redacted> --from-literal=prod-token=<redacted>"
  exit 0
fi

if k -n "$ns" get secret alertmanager-webhook > /dev/null 2>&1; then
  echo "secret alertmanager-webhook already exists in $ns, left as it is"
  exit 0
fi

token() {
  local env_name="$1" b64
  b64=$(k -n "runwatch-$env_name" get secret agent -o 'jsonpath={.data.webhook-token}' 2> /dev/null) || b64=""
  if [ -z "$b64" ]; then
    echo "Secret agent has no webhook-token in runwatch-$env_name: run 'make k8s-secrets ENV=$env_name'" >&2
    return 1
  fi
  printf '%s' "$b64" | base64 -d
}

dev_token=$(token dev)
prod_token=$(token prod)
k -n "$ns" create secret generic alertmanager-webhook \
  --from-literal "dev-token=$dev_token" --from-literal "prod-token=$prod_token" > /dev/null
echo "created secret alertmanager-webhook in $ns"
