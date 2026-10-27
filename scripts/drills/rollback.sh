#!/usr/bin/env bash
# Undo the last rollout of the app Deployments (path 1 of docs/runbooks/rollback.md).
# Usage: make rollback ENV=<dev|prod> [DEPLOY=<name>] [REVISION=<n>] [CONFIRM=yes]
# DRY_RUN=1 prints the commands instead of running them.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

usage() {
  echo "usage: make rollback ENV=<dev|prod> [DEPLOY=<name>] [REVISION=<n>] [CONFIRM=yes (prod)]" >&2
  exit 2
}

all=(api worker frontend loadgen agent otel-collector)
env_name="${ENV:-}"
deploy="${DEPLOY:-}"
revision="${REVISION:-}"

case "$env_name" in dev|prod) ;; *) usage ;; esac
if [ "$env_name" = "prod" ] && [ "${CONFIRM:-}" != "yes" ]; then
  echo "rolling back prod needs CONFIRM=yes" >&2
  usage
fi
targets=("${all[@]}")
if [ -n "$deploy" ]; then
  found=""
  for d in "${all[@]}"; do [ "$d" = "$deploy" ] && found=1; done
  if [ -z "$found" ]; then
    echo "DEPLOY must be one of: ${all[*]}" >&2
    exit 2
  fi
  targets=("$deploy")
fi
if [ -n "$revision" ]; then
  [ -n "$deploy" ] || { echo "REVISION needs DEPLOY" >&2; usage; }
  case "$revision" in *[!0-9]*) echo "REVISION must be a number" >&2; usage ;; esac
fi

drill_init "$env_name"

undone=()
skipped=()
for name in "${targets[@]}"; do
  echo "== $name"
  kc rollout history "deployment/$name"
  undo_args=(rollout undo "deployment/$name")
  [ -n "$revision" ] && undo_args+=("--to-revision=$revision")
  if [ "${DRY_RUN:-}" = "1" ]; then
    kc "${undo_args[@]}"
  else
    set +e
    out="$(kc "${undo_args[@]}" 2>&1)"
    rc=$?
    set -e
    echo "$out"
    if [ "$rc" -ne 0 ]; then
      if echo "$out" | grep -qiE 'no rollout history|no previous revision|unable to find specified revision'; then
        echo "$name: skipped: no previous revision"
        skipped+=("$name")
        continue
      fi
      exit "$rc"
    fi
  fi
  kc rollout status "deployment/$name" --timeout=180s
  undone+=("$name")
done

echo
echo "rollback summary for $NS"
echo "  undone:  ${undone[*]:-none}"
echo "  skipped: ${skipped[*]:-none} (no previous revision)"
echo "The cluster now differs from main. Open a revert PR on main (git revert <bad commit>)"
echo "and merge it before any other change, or CD redeploys the bad release."
echo "See docs/runbooks/rollback.md."
