# Incident 20261028T053658Z-ded192373bcb62d6

## Summary

- alertname: JobFailureRateHigh
- env: dev
- fingerprint: ded192373bcb62d6
- started: 2026-10-28T05:36:58Z
- ended: 2026-10-28T05:36:58Z
- duration: 0s
- state: finished
- outcome: skipped_no_model

## Alert

- alertname: JobFailureRateHigh
- severity: critical
- summary: Jobs have started failing, so users see their jobs end in failure.
- description: The share of attempts in the last 5 minutes that failed for infrastructure reasons is 0.7692307692307692, above 0.5 (at least 5 attempts).
- startsAt: 2026-10-28T05:36:43.406Z
- labels: alertname=JobFailureRateHigh, env=dev, service=runwatch, severity=critical

## Timeline

- 2026-10-28T05:36:58Z first firing: JobFailureRateHigh
- 2026-10-28T05:36:58Z webhook firing: started
- 2026-10-28T05:36:58Z run ended: skipped_no_model
- 2026-10-28T05:41:18Z webhook resolved: recorded_resolved

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
