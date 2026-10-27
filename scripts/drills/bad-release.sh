#!/usr/bin/env bash
# Incident 2: roll out a worker revision with WORKER_FAULT=fail_all in runwatch-dev.
# Usage: make bad-release   (DRY_RUN=1 prints the commands instead of running them)
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
drills_dev_only

if [ "${DRY_RUN:-}" != "1" ]; then
  replicas="$(kc_read get deployment/worker -o 'jsonpath={.spec.replicas}')"
  if [ "$replicas" = "0" ]; then
    echo "worker is scaled to 0, run make heal first" >&2
    exit 1
  fi
  current="$(kc_read get deployment/worker -o 'jsonpath={.spec.template.spec.containers[0].env[?(@.name=="WORKER_FAULT")].value}')"
  if [ "$current" = "fail_all" ]; then
    echo "bad-release: WORKER_FAULT=fail_all is already set, no new revision."
    exit 0
  fi
fi

# The annotation goes first: the Deployment controller copies it onto the new
# ReplicaSet, and the agent's k8s_rollout_history reads it from there.
kc annotate deployment/worker kubernetes.io/change-cause="bad-release drill: WORKER_FAULT=fail_all" --overwrite
kc set env deployment/worker WORKER_FAULT=fail_all

cat <<MSG
bad-release: new worker revision with WORKER_FAULT=fail_all in $NS.
Expect: JobFailureRateHigh within 5 minutes.
The agent should undo the worker's rollout and requeue the infra-failed jobs;
make rollback ENV=dev or make heal does it by hand.
MSG
