# Incident drills, no-model run

These are **no-model incidents**: the agent ran without `ANTHROPIC_API_KEY`, so
every incident here ended `skipped_no_model` and **the agent fixed nothing**.
The faults were set up with `make break-worker` and `make bad-release`, and
cleared by hand (`make heal`, `make rollback ENV=dev DEPLOY=worker`, requeue
calls). The reports are copied unedited from the dev agent. What this proves:
the alerts fire in time, Alertmanager delivers to the dev agent, the agent
records a report for every alert, and the manual recovery works. It does not
prove the agent's diagnosis or fixes (AC-21, AC-22), which are owner-only below.

Times are UTC, 2026-10-28, from real command output and Prometheus
`/api/v1/alerts`; full notes and load averages are in
[docs/live-checks.md](../docs/live-checks.md). Each directory holds
`response.md` (the report from `GET /incidents/<id>`), `status.json` and
`context.md`; there is no `actions.jsonl` or `summary.md` because the agent took
no action. Environment: kind cluster, namespace `runwatch-dev`, images of `main`
at `aa3de8f`.

| Drill | Command start | Alert firing (Prometheus `activeAt` + 1 m for) | Incident | Outcome |
| --- | --- | --- | --- | --- |
| Routing smoke | `POST /api/v2/alerts` 04:36:22 | `RoutingSmoke` (posted by hand) | [20261028T043637Z-74793167eb945180](20261028T043637Z-74793167eb945180/response.md) | `skipped_no_model` |
| 1, take 1 (idle worker) | `make break-worker` 04:47:31 | `QueueWaitHigh` activeAt 04:49:43, firing 04:50:43 (3 m 12 s); `JobsStalled` never fired | [20261028T045058Z-70d96fc8d310e3d7](20261028T045058Z-70d96fc8d310e3d7/response.md) | `skipped_no_model` |
| 1, take 2 (job running) | `make break-worker` 04:55:35 | `JobsStalled` activeAt 04:56:13, firing 04:57:13 (1 m 38 s); `QueueWaitHigh` activeAt 04:57:28, firing 04:58:28 (2 m 53 s) | [20261028T045728Z-b497628b3c90a3ca](20261028T045728Z-b497628b3c90a3ca/response.md) (`JobsStalled`); the `QueueWaitHigh` repeat went into the take 1 incident as `recorded_cooldown` | `skipped_no_model` |
| 2 | `make bad-release` 05:32:58 | `JobFailureRateHigh` activeAt 05:35:43, firing 05:36:43 (3 m 45 s) | [20261028T053658Z-ded192373bcb62d6](20261028T053658Z-ded192373bcb62d6/response.md) | `skipped_no_model` |

Recovery times (command start to the Alertmanager resolved webhook): `make heal`
at 05:12:21 to `JobsStalled` resolved 05:12:58 (37 s); `QueueWaitHigh` resolved
05:31:43 (19 m 22 s, a single worker did not drain the backlog, a second one
was added for 5 minutes); `make rollback ENV=dev DEPLOY=worker` at 05:37:31 to
`JobFailureRateHigh` resolved 05:41:18 (3 m 47 s).

Other incidents that appeared in the dev agent during the run
(`RunwatchApiDown` while the cluster restarted, repeated `QueueWaitHigh` and
`JobsStalled` after the cooldown) are not copied.

## Owner-only (not run)

None of this was run by an agent, none of it blocks the MVP, and the no-model
evidence above is complete without it. Run it later at your own expense and
discretion.

1. **Live-model drill (AC-21, AC-22, and AC-23/AC-25 with real runs).** Claude
   diagnoses and applies the fix with tool use against the real Anthropic API.
   - Cost: metered Anthropic API usage on your account. Each run is capped in
     the agent (#28) at 20 model calls, 3 write actions and 200k tokens. To
     limit spend set `AGENT_MODEL` to a cheaper model
     (`kubectl set env deployment/agent AGENT_MODEL=<model> -n runwatch-dev`)
     and give the key a spend limit in the Anthropic console. Expect a handful
     of runs (one per fired alert, two or three per drill).
   - Commands (dev only, Compose stopped, `make kind-up`, dev deployed as in
     `docs/runbooks/deploy.md`, `--kubeconfig ~/.kube/runwatch-kind.config
     --context kind-runwatch` on every kubectl): install the key from your
     environment into the dev Secret from stdin (`kubectl patch secret agent -n
     runwatch-dev --patch-file /dev/stdin` fed by `printf` and `base64`, never on
     a command line or in a file), then `kubectl rollout restart
     deployment/agent -n runwatch-dev`. Set `kubectl set env deployment/agent
     AGENT_VERIFY_TIMEOUT_S=900 -n runwatch-dev` (the 10-minute stats window can
     otherwise end a correctly fixed bad-release incident as `escalated`; root
     cause is #75). Before drill 1 check that the otel-collector is not
     thrashing at its 96Mi limit (see `docs/live-checks.md`, "Deviations").
     Drill 1: start a long job so one is running, `make break-worker`, expect
     the agent to scale the worker and requeue stalled jobs, queue drains,
     alerts resolve. Wait at least 10 minutes (cooldown), no dev alert firing.
     Drill 2: `make bad-release`, expect `k8s_rollout_undo` of the worker and
     `requeue_failed_jobs`, new jobs `SUCCEEDED`, alert resolves, the report
     carries the revert-on-`main` note. Afterwards `make heal`.
   - Evidence to record: per incident the files `response.md`, `status.json`,
     `context.md`, `actions.jsonl`, `summary.md` copied into `incidents/<id>/`,
     the table of takes (drill, take, start time, firing times, incident id,
     outcome, number of `actions.jsonl` lines), the model id, the number of
     runs (the agent records no token counts), and the report path in
     "Exercised rollbacks" of `docs/runbooks/rollback.md`.
2. **Prod environment drill.** Deploy prod (`make deploy ENV=prod SHA=<40-hex>`,
   or CD with the `prod` approval) with dev scaled to 0 first so the two never
   run together (prod is currently at 0 replicas, scale it up or redeploy);
   check `prod.localhost` returns 200, prod series appear with `env="prod"`,
   `RoutingSmoke` with `env=prod` reaches the prod agent (`skipped_no_model`
   without a key), and a dev fault leaves prod's
   `runwatch_attempts_finished_total{cause="infra"}` increase at 0. No cost
   beyond VM load.
3. **CD-driven redeploy, rollback path 2 (AC-28): done for dev (2026-10-28).**
   `gh workflow run CD --ref main -f environment=dev -f sha=<earlier 40-hex>`
   succeeded on the runner ([run 40687761011](https://github.com/coalesce8/ai-dev-tools-zoomcamp-project1/actions/runs/40687761011)):
   the dev pods ran that SHA's images and `rollout history` showed `deploy
   9fcb022 by 40687761011`; see "Exercised rollbacks" in
   `docs/runbooks/rollback.md`. A prod redeploy through CD is still
   owner-only (item 2).
4. **Timings only the live agent can produce**: time from alert to the agent's
   first write, time to resolve with the agent's fix, and the verification
   outcome. The no-model timings above are the baseline.
