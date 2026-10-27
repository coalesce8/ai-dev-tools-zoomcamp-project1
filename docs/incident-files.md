# Incident directory files

Each incident is a directory under `AGENT_INCIDENTS_DIR`, named
`<UTC yyyymmddThhmmssZ>-<fingerprint>`.

| File | Written by | Content |
| --- | --- | --- |
| `alert.json` | webhook handler | the first alert, as received |
| `context.md` | webhook handler, tools | header and notes; never copied into the report |
| `status.json` | run controller | `incident_id`, `fingerprint`, `alertname`, `env`, `state` (`running`/`finished`), `outcome`, `started_at`, `ended_at`, `webhook_count`; `interrupted: true` when a restart closed the run |
| `webhooks.jsonl` | webhook handler | one line per webhook: `received_at`, `kind`, `disposition` |
| `actions.jsonl` | tool-use loop (#28) | see below; optional |
| `summary.md` | tool-use loop (#28) | the model's final answer; optional |
| `response.md` | run controller | the Markdown report, written atomically at run end, on recovery and on a repeated webhook; empty while running |

The report (`runwatch/agent/reports.py`) is built only from these structured
fields. It never embeds `context.md`, tool results, job names or log lines, and
a redaction pass removes configured secrets and secret-shaped strings.

## `actions.jsonl`

One JSON object per line, appended in order, exactly these keys (extra keys are
ignored):

```json
{"ts": "2026-03-01T12:01:00Z", "kind": "tool", "tool": "k8s_rollout_undo",
 "args": {"deployment": "worker"}, "write": true, "ok": true,
 "summary": "rolled back to revision 4"}
```

- `ts`: ISO-8601 UTC. `kind`: `tool` or `verification`.
- `args` holds only `deployment`, `replicas`, `since`, `until`, `state`, `app`,
  `id`; `query_loki` is `{"query_chars": N}` (the LogQL is never stored).
- One line per tool call the model emitted, refused calls included (`ok` false,
  `summary` "refused by tool rules"), and one per verification poll.
- `summary`: at most 200 characters, counts and ids only, never job names, log
  content or raw tool results.
- For `kind: "verification"`, `tool` is `get_stats` and `ok` means the alert
  condition cleared.
- A line that is not JSON or lacks `ts`/`tool` is skipped and counted in the
  report as "N unreadable entries".

## `summary.md`

The model's final answer, cut at 8000 characters (the report shows the first
2000, labelled model-written and unverified). Absent when the run ended without
final model text.
