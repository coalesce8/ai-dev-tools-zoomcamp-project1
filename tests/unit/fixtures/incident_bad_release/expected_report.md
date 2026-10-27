# Incident 20260301T120000Z-bad1

## Summary

- alertname: HighInfraFailureRate
- env: dev
- fingerprint: bad1
- started: 2026-03-01T12:00:00Z
- ended: 2026-03-01T12:03:40Z
- duration: 3m 40s
- state: finished
- outcome: resolved

## Alert

- alertname: HighInfraFailureRate
- severity: critical
- summary: Infra failure rate is high
- description: Fixture alert for HighInfraFailureRate
- startsAt: 2026-03-01T12:00:00Z
- labels: alertname=HighInfraFailureRate, env=dev, severity=critical

## Timeline

- 2026-03-01T12:00:00Z first firing: HighInfraFailureRate
- 2026-03-01T12:00:00Z webhook firing: started
- 2026-03-01T12:00:10Z `get_stats`: ok
- 2026-03-01T12:00:20Z `k8s_rollout_history` (deployment=worker): ok
- 2026-03-01T12:00:30Z `query_loki` (query, 86 chars): ok
- 2026-03-01T12:01:00Z `k8s_rollout_undo` (deployment=worker): ok
- 2026-03-01T12:01:20Z `requeue_failed_jobs` (since=2026-03-01T11:58:41Z, until=2026-03-01T12:01:20Z): ok
- 2026-03-01T12:01:30Z `k8s_scale` (deployment=worker, replicas=9): refused
- 2026-03-01T12:02:30Z verification: condition still true
- 2026-03-01T12:03:00Z webhook firing: recorded_in_progress
- 2026-03-01T12:03:30Z verification: condition cleared
- 2026-03-01T12:03:40Z run ended: resolved
- 2026-03-01T12:04:30Z webhook firing: recorded_in_progress
- 2026-03-01T12:09:00Z webhook firing: recorded_cooldown
- 2026-03-01T12:10:00Z webhook resolved: recorded_resolved

## Evidence

- `get_stats`: ok - infra failure rate 0.92, 14 jobs queued
- `k8s_rollout_history` (deployment=worker): ok - revision 5 created 11:58:41Z, image tag abc123
- `query_loki` (query, 86 chars): ok - 40 lines, 12 error lines

## Actions taken

- `k8s_rollout_undo` (deployment=worker): ok - rolled back to revision 4
- `requeue_failed_jobs` (since=2026-03-01T11:58:41Z, until=2026-03-01T12:01:20Z): ok - 9 jobs requeued
- `k8s_scale` (deployment=worker, replicas=9): refused - refused by tool rules

The cluster now differs from `main`: `main` needs a revert PR before the next deploy, because CD would otherwise roll the bad release out again.

## Outcome

- outcome: resolved
- last verification: condition cleared at 2026-03-01T12:03:30Z

Agent summary (model-written, unverified):

> Rolled back the worker and requeued 9 jobs.

## What a human should check

The alert condition was clear in a verification poll. Confirm the alert is resolved and the queue is draining.

The cluster now differs from `main`: `main` needs a revert PR before the next deploy, because CD would otherwise roll the bad release out again.

## Repeated webhooks

- webhook_count: 5
- dispositions: 2 recorded_in_progress, 1 recorded_cooldown, 1 recorded_resolved
