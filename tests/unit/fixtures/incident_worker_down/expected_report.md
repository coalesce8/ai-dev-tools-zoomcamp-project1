# Incident 20260301T140000Z-wd01

## Summary

- alertname: WorkerDown
- env: dev
- fingerprint: wd01
- started: 2026-03-01T14:00:00Z
- ended: 2026-03-01T14:02:40Z
- duration: 2m 40s
- state: finished
- outcome: resolved

## Alert

- alertname: WorkerDown
- severity: critical
- summary: No worker is running
- description: Fixture alert for WorkerDown
- startsAt: 2026-03-01T14:00:00Z
- labels: alertname=WorkerDown, env=dev, severity=critical

## Timeline

- 2026-03-01T14:00:00Z first firing: WorkerDown
- 2026-03-01T14:00:00Z webhook firing: started
- 2026-03-01T14:00:10Z `get_stats`: ok
- 2026-03-01T14:00:20Z `k8s_pods` (app=worker): ok
- 2026-03-01T14:00:40Z `k8s_scale` (deployment=worker, replicas=1): ok
- 2026-03-01T14:01:00Z `requeue_stalled_jobs`: ok
- 2026-03-01T14:02:00Z webhook firing: recorded_in_progress
- 2026-03-01T14:02:30Z verification: condition cleared
- 2026-03-01T14:02:40Z run ended: resolved

## Evidence

- `get_stats`: ok - 0 workers, 31 jobs queued
- `k8s_pods` (app=worker): ok - 0 worker pods

## Actions taken

- `k8s_scale` (deployment=worker, replicas=1): ok - worker scaled to 1
- `requeue_stalled_jobs`: ok - 4 jobs requeued

## Outcome

- outcome: resolved
- last verification: condition cleared at 2026-03-01T14:02:30Z

Agent summary (model-written, unverified):

> Scaled the worker to 1.

## What a human should check

The alert condition was clear in a verification poll. Confirm the alert is resolved and the queue is draining.

## Repeated webhooks

- webhook_count: 2
- dispositions: 1 recorded_in_progress
