# Incident 20261028T045058Z-70d96fc8d310e3d7

## Summary

- alertname: QueueWaitHigh
- env: dev
- fingerprint: 70d96fc8d310e3d7
- started: 2026-10-28T04:50:58Z
- ended: 2026-10-28T04:50:58Z
- duration: 0s
- state: finished
- outcome: skipped_no_model

## Alert

- alertname: QueueWaitHigh
- severity: critical
- summary: Jobs are not starting, so users wait for their jobs to begin.
- description: The oldest queued job has waited 179.484171 seconds, above the 120 second limit.
- startsAt: 2026-10-28T04:50:43.406Z
- labels: alertname=QueueWaitHigh, env=dev, service=runwatch, severity=critical

## Timeline

- 2026-10-28T04:50:58Z first firing: QueueWaitHigh
- 2026-10-28T04:50:58Z webhook firing: started
- 2026-10-28T04:50:58Z run ended: skipped_no_model
- 2026-10-28T04:53:28Z webhook resolved: recorded_resolved
- 2026-10-28T04:58:43Z webhook firing: recorded_cooldown

## Evidence

None recorded.

## Actions taken

None recorded.

## Outcome

- outcome: skipped_no_model

## What a human should check

No `ANTHROPIC_API_KEY` was set, so nobody handled this alert. Handle it by hand.

## Repeated webhooks

- webhook_count: 3
- dispositions: 1 recorded_cooldown, 1 recorded_resolved
