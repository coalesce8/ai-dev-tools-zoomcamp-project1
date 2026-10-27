#!/usr/bin/env bash
# Incident 1: scale the worker to 0 in runwatch-dev. Idempotent.
# Usage: make break-worker   (DRY_RUN=1 prints the command instead of running it)
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
drills_dev_only

kc scale deployment/worker --replicas=0

cat <<MSG
break-worker: the worker is at 0 replicas in $NS.
Expect: JobsStalled within 2 minutes, QueueWaitHigh within 4 minutes.
The agent should scale the worker back up; make heal restores it by hand.
MSG
