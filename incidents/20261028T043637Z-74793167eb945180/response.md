# Incident 20261028T043637Z-74793167eb945180

## Summary

- alertname: RoutingSmoke
- env: dev
- fingerprint: 74793167eb945180
- started: 2026-10-28T04:36:37Z
- ended: 2026-10-28T04:36:37Z
- duration: 0s
- state: finished
- outcome: skipped_no_model

## Alert

- alertname: RoutingSmoke
- severity: critical
- summary: routing smoke test
- description: 
- startsAt: 2026-10-28T04:36:22Z
- labels: alertname=RoutingSmoke, env=dev, service=runwatch, severity=critical

## Timeline

- 2026-10-28T04:36:37Z first firing: RoutingSmoke
- 2026-10-28T04:36:37Z webhook firing: started
- 2026-10-28T04:36:37Z run ended: skipped_no_model
- 2026-10-28T04:40:37Z webhook resolved: recorded_resolved

## Evidence

None recorded.

## Actions taken

None recorded.

## Outcome

- outcome: skipped_no_model

## What a human should check

No `ANTHROPIC_API_KEY` was set, so nobody handled this alert. Handle it by hand.

## Repeated webhooks

- webhook_count: 2
- dispositions: 1 recorded_resolved
