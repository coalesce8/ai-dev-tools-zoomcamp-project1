#!/usr/bin/env bash
# Create the Secrets the k8s manifests expect in runwatch-<env>: postgres and agent.
# Usage: scripts/k8s-secrets.sh <dev|prod>
# DRY_RUN=1 prints the commands with values redacted and runs nothing.
set -euo pipefail

env_name="${1:-}"
case "$env_name" in
  dev|prod) ;;
  *) echo "usage: $0 <dev|prod>" >&2; exit 2 ;;
esac
ns="runwatch-$env_name"
ctx="kind-runwatch"
kubeconfig="${KIND_KUBECONFIG:-$HOME/.kube/runwatch-kind.config}"

rand() { head -c 32 /dev/urandom | base64 | tr -d '/+=\n' | head -c 32; }

postgres_args=(--from-literal "password=<redacted>")
agent_args=(--from-literal "webhook-token=<redacted>")
[ -n "${ANTHROPIC_API_KEY:-}" ] && agent_args+=(--from-literal "anthropic-api-key=<redacted>")
[ -n "${GITHUB_TOKEN:-}" ] && agent_args+=(--from-literal "github-token=<redacted>")

if [ "${DRY_RUN:-}" = "1" ]; then
  echo "kubectl --context $ctx -n $ns create secret generic postgres ${postgres_args[*]} --dry-run=client -o yaml"
  echo "kubectl --context $ctx -n $ns create secret generic agent ${agent_args[*]} --dry-run=client -o yaml"
  exit 0
fi

k() { kubectl --kubeconfig "$kubeconfig" --context "$ctx" "$@"; }

create() {
  local name="$1"; shift
  if k -n "$ns" get secret "$name" >/dev/null 2>&1; then
    echo "secret $name already exists in $ns, left as it is"
    return
  fi
  k -n "$ns" create secret generic "$name" "$@" >/dev/null
  echo "created secret $name in $ns"
}

create postgres --from-literal "password=$(rand)"
agent_real=(--from-literal "webhook-token=$(rand)")
[ -n "${ANTHROPIC_API_KEY:-}" ] && agent_real+=(--from-literal "anthropic-api-key=$ANTHROPIC_API_KEY")
[ -n "${GITHUB_TOKEN:-}" ] && agent_real+=(--from-literal "github-token=$GITHUB_TOKEN")
create agent "${agent_real[@]}"
