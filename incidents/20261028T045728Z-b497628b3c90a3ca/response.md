# Incident 20261028T045728Z-b497628b3c90a3ca

## Summary

- alertname: JobsStalled
- env: dev
- fingerprint: b497628b3c90a3ca
- started: 2026-10-28T04:57:28Z
- ended: 2026-10-28T04:57:28Z
- duration: 0s
- state: finished
- outcome: skipped_no_model

## Alert

- alertname: JobsStalled
- severity: critical
- summary: Jobs have silently died, so users lose their running jobs.
- description: 1 jobs are in the STALLED state.
- startsAt: 2026-10-28T04:57:13.406Z
- labels: alertname=JobsStalled, env=dev, service=runwatch, severity=critical

## Timeline

- 2026-10-28T04:57:28Z first firing: JobsStalled
- 2026-10-28T04:57:28Z webhook firing: started
- 2026-10-28T04:57:28Z run ended: skipped_no_model

## Evidence

None recorded.

## Actions taken

None recorded.

## Outcome

- outcome: skipped_no_model

## What a human should check

No `ANTHROPIC_API_KEY` was set, so nobody handled this alert. Handle it by hand.

## Repeated webhooks

- webhook_count: 1
- dispositions: none
