#!/usr/bin/env bash
# Restore the healthy worker in runwatch-dev. Safe to run any number of times.
# Usage: make heal   (DRY_RUN=1 prints the commands instead of running them)
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
drills_dev_only

kc annotate deployment/worker kubernetes.io/change-cause="heal: drill state cleared" --overwrite
kc set env deployment/worker WORKER_FAULT-
kc scale deployment/worker --replicas=1
kc rollout status deployment/worker --timeout=120s

cat <<MSG
heal: the worker in $NS has no WORKER_FAULT and runs 1 replica.
It does not requeue jobs and does not resolve alerts by itself. Stalled or
infra-failed jobs from a hand-run drill are requeued in the UI or with
POST /api/v1/jobs/requeue-failed; alerts clear on their own once the
condition does.
MSG
