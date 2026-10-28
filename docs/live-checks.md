# Live checks on the kind cluster (dev, no model)

Run on 2026-10-28 (all times UTC; `uptime` prints local time, one hour ahead)
by the engineer of #37, against the kind cluster `runwatch`, namespace
`runwatch-dev` plus `runwatch-observability`, with images built locally from
`main` at `aa3de8f6feef8dcccd6149f638fe044f81856ad3` and deployed with
`SKIP_PULL=1 make deploy ENV=dev` (the deploy script annotates the change-cause "deploy aa3de8f by manual"; that line is not visible in the pasted history because `make heal` later overwrote the revision's cause, see Deviations).

**No model, no cost.** No `ANTHROPIC_API_KEY` was set, read or requested. The
Secret `agent` in `runwatch-dev` has only the key `webhook-token`
(`kubectl get secret agent -o jsonpath='{.data}'`, key names only), so the agent
logged "ANTHROPIC_API_KEY is not set: runs end as skipped_no_model" and every
incident below ended `skipped_no_model`. These drills prove the alerts, the
routing and the manual recovery, not the agent's diagnosis. Owner-only steps:
[incidents/README.md](../incidents/README.md).

**Prod.** `runwatch-prod` was already deployed when the cluster was started. Its
six Deployments and the `postgres` StatefulSet were at 1 replica each; they were
scaled to 0 (`kubectl scale`, nothing deleted) before the deploy and **prod was
left at 0**. Because of that, `RunwatchApiDown{env="prod"}` fires for the whole
run and its webhook to the prod agent fails; every "no dev alert" statement
below is about `env="dev"`.

Every `kubectl` call used `--kubeconfig ~/.kube/runwatch-kind.config --context
kind-runwatch`. `~/.kube/config` was not used (sha256 unchanged).

Load average (1 min) was recorded with `uptime`: 1.84 before `make kind-up`,
24.15 right after it (cluster start), 13.5 after the deploy, 3 to 6 at rest
with Grafana stopped, 2 to 4 during AC-20 and drill 1 take 1, 8 to 11 during drill 1 take 2 and drill 2 (see each row), 16.9 at the end
(05:57Z, Grafana and Tempo restarting) and 12.4 after `make kind-down`.

## Deviations and findings

- **otel-collector thrashes at the 96Mi limit (not fixed in the repo).** After the
  node restart the collector in `runwatch-dev` never listened on 4318 (the API
  logged "Connection refused" and Prometheus had no `runwatch_*` series). Its
  cgroup was at `memory.max` (96Mi) with 18 million `workingset_refault_file`
  events: the 165 MB `otelcol-contrib` binary's file pages do not fit next to
  the heap. 144Mi still thrashed (60 million refaults, 70 % of a core); 256Mi
  fixed it (refaults stop). I applied `kubectl set resources
  deploy/otel-collector --limits=memory=256Mi` in the live cluster only. A repo
  fix (`k8s/base/otel-collector.yaml`, `k8s/README.md`) would also have to change
  the budget assertion `total == 848` in `tests/unit/test_k8s_manifests.py`,
  which this issue may not touch, so none is committed. Follow-up: issue #104. A new `make deploy`
  resets the live limit to 96Mi and brings the problem back.
- **`/api/v1/jobs/requeue-failed` does not exist.** The issue and the runbook
  name it, but `requeue-failed` and `requeue-stalled` are internal endpoints
  (`/internal/v1/jobs/...`, 404 through the ingress by design). Stalled jobs were
  requeued with the public `POST /api/v1/jobs/{id}/requeue`; the infra-failed
  jobs with `POST /internal/v1/jobs/requeue-failed` through `kubectl
  port-forward svc/api` (the only way to reach it).
- **`make break-worker` stalls a job only if one is running.** The worker is
  stopped by SIGTERM; with no job running nothing becomes `STALLED` and
  `JobsStalled` never fires. Take 1 hit an idle worker (only `QueueWaitHigh`
  fired); take 2 started a 150 s job first.
- **One worker does not drain the backlog.** After `make heal` the worker
  (1 replica) kept the queue at 32 to 40 jobs, so `QueueWaitHigh` stayed firing.
  I scaled the worker to 2 for 5 minutes (05:27:22 to 05:32:31) and back to 1.
- **Change-cause is relabelled.** `heal` and `bad-release` annotate the
  Deployment before the pod template changes, so `rollout history` shows the
  previous revision under the new cause (revision 3 read "bad-release drill"
  although it was the good template). Cosmetic; not fixed here.
- Grafana (times from my notes, minute accuracy, partly converted from local
  `uptime` time): scaled to 0 at about 04:19Z while the collector was diagnosed,
  scaled to 1 at 04:23Z (it took until about 04:32Z to become Ready, load 15+),
  queried at 04:32:19Z (row 3b), scaled to 0 again at about 04:33Z, and back to 1
  at 05:46Z (Ready at about 05:55Z). So it was stopped during all the drills. A leftover Completed pod
  `wt37-curl` (busybox, `true`) in `runwatch-dev` was created by mistake; it
  needs `kubectl delete pod wt37-curl -n runwatch-dev` by the owner (agents delete
  nothing).

## Results

| # | Check | Command or URL | Observed | Load | Result |
| --- | --- | --- | --- | --- | --- |
| 1 | Pods Ready, ingress | `kubectl get pods` in `runwatch-dev` and `runwatch-observability`; `curl --resolve dev.localhost:80:127.0.0.1 http://dev.localhost/`, `/api/v1/stats`, `/internal/v1/jobs/requeue-failed` | all `1/1 Running` at 04:14Z (Grafana later: row 3); `/` 200 `text/html`; stats 200 JSON; internal path 404 | 6.80 | PASS |
| 2 | Prometheus series and rules (AC-17) | `localhost:9090/api/v1/query` per metric with `{env="dev"}`; `/api/v1/rules` | one series each for `runwatch_jobs_submitted_total`, `_jobs_finished_total`, `_attempts_finished_total`, `_queue_depth`, `_oldest_queued_age_seconds`, `_jobs_stalled`, `_queue_wait_seconds_*`, `_job_run_seconds_*`; no `_total_total` or `_seconds_seconds` name; `RunwatchApiDown` rule with `env="dev"` and `env="prod"`; 04:22Z, after the collector fix | 5.25 | PASS |
| 3 | AC-16 by HTTP | job `d29837ca-3adf-4213-b7d4-6fa6edac3abb` (duration 3) via the ingress; Tempo `localhost:3200/api/search?q={ span.job.id = "<id>" }` and `/api/traces/<id>`; Loki `query_range` | submit trace `cbe0c5800448d76adba7ff46f525e527` (`POST /api/v1/jobs`, `submit`, `claim`); attempt trace `8b12d9046e54fd51ff9da4eab8be6d77` (`attempt`, `run`, `result`, heartbeats) whose `attempt` span carries a link to `cbe0c580.../4ba49f705950714e`; LogQL `{service_name="runwatch-worker"} \| job_id="<id>"` 2 lines, `{service_name=~"runwatch-.*"} \| trace_id="8b12d904..."` 9 lines, `{service_name="runwatch-api"} \| job_id="<id>"` 6 lines | 5 | PASS |
| 3b | Dashboard exists (AC-17) | Grafana `localhost:3000/api/search?query=` (port-forward) | folder "Runwatch" and dashboard "Runwatch" (uid `runwatch`) at 04:32:19Z; Grafana 13.2.3 `database: ok` | 15 | PASS |
| 3c | Grafana panels look | Chrome via the extension, `http://localhost:3000/d/runwatch/runwatch` (port-forward `svc/grafana`), 08:55Z to 09:27Z, screenshots `wt37b-4a` to `wt37b-4e` in the scratchpad | Dashboard "Runwatch" (Grafana, dark theme, anonymous viewer, `env` variable All) has 11 panels: Jobs submitted, Jobs finished by outcome, Attempts finished by outcome and cause, Queue depth, Oldest queued job age, Stalled jobs, Queue wait p50/p95, Job run time p95, HTTP requests per second, HTTP latency p95, Logs. All 11 rendered with data and sensible titles, axes, units (ops/s, s, mins, req/s) and legends: two bursts of load-generator traffic with a gap where the VM was down, the earlier queue-wait and stalled-job spikes, route names with status codes in the HTTP legend; the Logs panel (15 min range, viewed alone) shows API log lines with `job_id` and no job names (`wt37b-4d`). **Caveat, honestly:** with the provisioned 10 s auto-refresh and a 1 h range the lower panels (queue wait, run time, HTTP, Logs) stayed empty with a spinner for minutes (`wt37b-4e`): Grafana's `/api/ds/query` calls took 12 to 35 s and were cancelled (status 499) by the next refresh; Prometheus (0.5 s) and Loki (0.6 s) answered the same queries fast when called directly. Only with auto-refresh switched off did the panels fill one by one (about 1 to 2 minutes), the Logs panel last (not within 3 min on the 1 h range, immediately on 15 min). The VM was thrashing (load 8 to 31, the kind node at 2 GiB of its 3 GiB limit with hundreds of GB of block reads, free memory about 200 MB) while Grafana loaded; idle baseline with dev and observability up and the browser idle was about 10 (`uptime` 9.7 to 12 at 09:2xZ), it never settled below 4 after the first dashboard load. Not a dashboard defect proven; the dashboard is usable but slow on this VM, and 10 s refresh makes it worse | 3.6 at start, 8 to 31 while loading, about 10 idle | PASS (look); slow under VM load, see caveat |
| 4 | Alertmanager routing to the dev agent | `POST localhost:9093/api/v2/alerts` `RoutingSmoke` `env=dev` at 04:36:22Z; `GET /incidents`, `GET /incidents/<id>` via port-forward `svc/agent`; `POST /webhook/alertmanager` without a token | agent answered; incident `20261028T043637Z-74793167eb945180` created 04:36:37Z (15 s), outcome `skipped_no_model`, report renders; no token gives 401 | 5.5 | PASS |
| 5 | AC-20 | 10 `always_fail` jobs (`duration_s` 1) at 04:37:30Z; Prometheus `/api/v1/alerts` and rule state every 30 s to 04:45:04Z (17 polls, load 1.9 to 4.2) | all 10 `FAILED` (3 attempts each); no dev alert pending or firing in any poll; `JobFailureRateHigh` rule `inactive` throughout | 2 to 4 | PASS |
| 6 | Drill 1, take 1, `make break-worker` at 04:47:31Z (idle worker) | monitor every 10 s | `QueueWaitHigh` activeAt 04:49:43Z, `firing` 04:50:43Z (3 m 12 s after the command; seen at the 04:50:52 poll); `JobsStalled` never fired (no running job, see deviations); incident `20261028T045058Z-70d96fc8d310e3d7` (webhook 04:50:58Z, 202); `make heal` at 04:51:05Z; resolved webhook 04:53:28Z (2 m 23 s after heal, queue was short) | 2.3 | PASS for `QueueWaitHigh`; `JobsStalled` NOT shown |
| 7 | Drill 1, take 2, `make break-worker` at 04:55:35Z with a 150 s job RUNNING | monitor every 10 s | `JobsStalled` activeAt 04:56:13Z, `firing` 04:57:13Z (1 m 38 s after the command); `QueueWaitHigh` activeAt 04:57:28Z, `firing` 04:58:28Z (2 m 53 s). Agent: `JobsStalled` incident `20261028T045728Z-b497628b3c90a3ca` (04:57:28Z, outcome `skipped_no_model`); the `QueueWaitHigh` firing at 04:58:43Z was recorded in the existing incident `...045058Z-70d9...` as `recorded_cooldown` (same fingerprint, still in cooldown), no new directory | 8.0 to 10.8 | PASS (AC-19) |
| 8 | Drill 1 recovery | `make heal` at 05:12:21Z (done 05:12:30Z); `POST /api/v1/jobs/<id>/requeue` for the one `STALLED` job (200 at 05:12:30Z) | `JobsStalled` resolved webhook 05:12:58Z (37 s after heal start). Queue then stayed at 32 to 40 with one worker; scaled the worker to 2 (05:27:22Z), queue 0 at 05:32:24Z, back to 1 at 05:32:31Z, no dev alert; `QueueWaitHigh` resolved webhook 05:31:43Z (19 m 22 s after heal start). `/api/v1/stats` showed `STALLED` 0 | 9 to 10.5 | PASS (with the extra worker) |
| 9 | Drill 2, `make bad-release` at 05:32:58Z | monitor every 10 s | `JobFailureRateHigh` activeAt 05:35:43Z, `firing` 05:36:43Z (3 m 45 s, AC-19 within 5 min); incident `20261028T053658Z-ded192373bcb62d6` at 05:36:58Z, outcome `skipped_no_model`; state kept | 11.6 | PASS |
| 10 | Exercised rollback (AC-28 path 1) | `make rollback ENV=dev DEPLOY=worker` at 05:37:31Z (done 05:37:39Z), output below | history before: revisions 1, 2, 3, 4 (bad); after: 1, 2, 4, 5 (revision 3 re-created as 5); `WORKER_FAULT` no longer in the worker env (env names: `RUNWATCH_API_URL`, `OTEL_EXPORTER_OTLP_ENDPOINT`); `POST /internal/v1/jobs/requeue-failed` since 05:32:00Z until 05:37:50Z returned `{"requeued":14}`; per-job states of the 14 were not recorded; the stats at 05:42:09Z showed `FAILED` 9 (all from row 5, none new), 4 `QUEUED`, no `STALLED`, and the new jobs at 05:45Z (UI check, row 15) `SUCCEEDED`; `JobFailureRateHigh` last seen firing 05:40:51Z, resolved webhook 05:41:18Z (3 m 47 s after the rollback command). The full six-Deployment `make rollback ENV=dev` was NOT run live (it would move api/frontend/agent to older SHAs); #36 covers it offline. A side effect of requeuing 14 jobs: `QueueWaitHigh` fired 05:39:58Z and resolved on its own (incident `20261028T054113Z-70d96fc8d310e3d7`) | 8.4 to 10.6 | PASS |
| 11 | `make heal` after drill 2 | `make heal` at 05:42:09Z | worker 1/1, no `WORKER_FAULT`, all six Deployments ready, no dev alert | 11.2 | PASS |
| 12 | AC-25 as far as no-model shows it | `webhooks.jsonl` of the incidents (`kubectl exec deploy/agent -- cat`) | repeated webhook inside the cooldown: `QueueWaitHigh` incident `...045058Z-70d9...` lists `{"kind": "firing", "disposition": "recorded_cooldown"}` at 04:58:43Z and no new directory was created for it. Alertmanager's `repeat_interval` (10 m) equals the agent's cooldown, so the resends at 05:07:58Z (`JobsStalled`) and 05:09:13Z (`QueueWaitHigh`) fell outside it and started new incidents (`...050758Z-b497...`, `...050913Z-70d9...`) by design. A repeat for a run still in progress needs a run that takes time, i.e. a live model | | PASS (cooldown path); in-progress path NEEDS OWNER |
| 13 | Frontend shell and API lifecycle via `curl` | `GET /`; assets; `POST /api/v1/jobs` (job `3e84a392-8256-4fe4-9597-1d5ff8c77711`, duration 6); `/logs?after=` | `/` 200 `text/html`; `/assets/index-DTbJHRpu.js` 200 `application/javascript`, `/assets/index-RZB2mTII.css` 200 `text/css`; job listed in `GET /api/v1/jobs`; QUEUED 04:37:17Z, RUNNING 04:37:39Z, SUCCEEDED 04:37:45Z; logs seq `[1]`, `[2,3]`, `[4,5]`, `[6]` | 4.5 | PASS |
| 14 | AC-15 requeue and cancel rules | `POST /api/v1/jobs/{id}/requeue` and `/cancel` | `SUCCEEDED` requeue 409, cancel 409; `FAILED` requeue 200 (QUEUED); requeue of a RUNNING job 409; cancel 200 (CANCELLED); CANCELLED requeue 409, cancel 409 | 2.4 | PASS |
| 15 | Browser checks (AC-13, AC-14, AC-15) | Chrome via the connected extension, `http://dev.localhost/`, one tab, closed afterwards | Form with empty name and duration 0 shows "Name is required" and "Duration must be between 1 and 600 seconds" and sends nothing (PASS, AC-14). A valid job `ui-check` (duration 20) appeared in the table and showed `RUNNING` without a reload (PASS, AC-13); the detail page showed `RUNNING` with only a Cancel button and no Requeue, then `SUCCEEDED` with attempt 1 and all 20 log lines `step n/20` (PASS, AC-15, buttons by state). Stats counters updated by themselves | 11 | PASS for those |
| 15b | Browser checks (scroll-follow, API error, Requeue on FAILED), AC-13, AC-14, AC-15 | Chrome via the extension, `http://dev.localhost/`, one tab, started with load 3.6 (09:49 local) | (1) **Scroll-follow, PASS**: UI-submitted job `scroll-check` (duration 60, job `7e247fd2-2641-4bc8-bec2-db36f2924b4c`), detail page, `.log-box` (max-height 24 rem): while lines arrived the box stayed at the bottom (scrollTop equal to scrollHeight minus clientHeight, gap 0, 41 then 45 lines); after scrolling the box up to the top it stayed there while lines kept arriving (gap grew 287 to 377 px, 45 to 51 lines); after scrolling back to the bottom it followed again (gap 0, 55 lines); the job ended `SUCCEEDED` (`wt37b-1`). (2) **API error display, PASS**: `kubectl scale deploy/api -n runwatch-dev --replicas=0` at 08:50:59Z, reloaded the page, submitted the form: "Request failed with status 503" in red above the submit button and also under the form and the state filter where the list could not load (`wt37b-2`); API back to 1 replica at 08:51:19Z, rollout done, `/api/v1/stats` 200 eight times in a row. (3) **Requeue on FAILED, PASS**: detail page of FAILED job `d103e1c8-db40-45a8-9bbb-3f9afe6d5fb0` (3/3 attempts, last error `always_fail mode`) shows exactly one button, Requeue (`wt37b-3a`); clicking it changed the page to `QUEUED`, attempts 0/3, and the button to Cancel (`wt37b-3b`); the always-fail job then ran again and ended `FAILED` 3/3, as expected. Shown by the earlier row 15: no Requeue on RUNNING or SUCCEEDED. The page look (plain unstyled layout, readable, no overflow at 1213 px) was seen in every screenshot. Nothing else in the checklist remains | 3.6, 2 during the checks | PASS |
| 16 | AC-9 on kind | job `6927ab0a-2940-4edd-b7e5-203703cbf2b5` (duration 40); first log line at 04:46:06Z; `kubectl rollout restart statefulset/postgres -n runwatch-dev` at 04:46:06.4Z (PVC `data-postgres-0` Bound); `/readyz` polled every 0.5 s inside the API pod | `/readyz` not 200 from 04:46:07.158Z (503, then a timeout, then 503) to 04:46:10.702Z: last 200 at 04:46:06.648Z, first 200 again at 04:46:10.702Z, so an outage of about 4 s (sampling 0.5 s); the job ended `SUCCEEDED` with 1 attempt | 1.8 | PASS |
| 17 | Agent restarts cleanly | `kubectl rollout restart deployment/agent -n runwatch-dev` at 05:42:19Z | ready 05:42:52Z; `GET /incidents` listed 24 incidents before and 24 after; startup warning "ANTHROPIC_API_KEY is not set" | 11.2 | PASS |

### Row 10 output (`make rollback ENV=dev DEPLOY=worker`)

```
== worker
REVISION  CHANGE-CAUSE
1         deploy 2ba608f by manual
2         deploy a30431e by manual
3         bad-release drill: WORKER_FAULT=fail_all
4         bad-release drill: WORKER_FAULT=fail_all

deployment.apps/worker rolled back
deployment "worker" successfully rolled out

rollback summary for runwatch-dev
  undone:  worker
  skipped: none (no previous revision)

history after:
REVISION  CHANGE-CAUSE
1         deploy 2ba608f by manual
2         deploy a30431e by manual
4         bad-release drill: WORKER_FAULT=fail_all
5         bad-release drill: WORKER_FAULT=fail_all
```

(Revision 3 was the good template; its change-cause was overwritten, see
"Change-cause is relabelled".)

## Cleanup state

`make heal` was run after drill 2 (row 11): worker 1 replica without
`WORKER_FAULT`, agent 1 replica, no dev alert. Grafana was scaled back to 1.
Prod stays at 0 replicas. At the end Tempo's readiness probe (1 s timeout) was
failing under the load of Grafana's restart (`0/1`, 803 failures in 65 minutes
in the pod events); traces were still served. `make kind-down` was run at the end; Compose was never
started.

### Browser rows (3c and 15b), second session

Done on 2026-10-28 from about 08:49Z to 09:27Z with the Chrome extension
connected, after a VM restart (7.4 GB RAM). The existing dev stack and
observability on the kind cluster were brought up with `make kind-up` (the
Deployments were already rolled out from the first session, so `make deploy`
was not repeated; prod stayed at 0 replicas and was not deployed). Load
average (`/proc/loadavg`, 1 min): 42 right after `make kind-up` (cluster
start), 3.56 at 08:49Z when the UI checks began, 2 to 3 during rows 15b (1) to
(3), then 8 to 31 while Grafana loaded the dashboard and about 10 idle
afterwards. Screenshots are kept outside the repo in the session scratchpad
(`wt37b-1-logbox-follows.jpg`, `wt37b-2-api-503-error.jpg`,
`wt37b-3a-failed-requeue-button.jpg`, `wt37b-3b-after-requeue.jpg`,
`wt37b-4a` to `wt37b-4e` for Grafana). The API was scaled to 0 for 20 s for the
error check and back to 1; the always-fail job `d103e1c8...` was requeued once
and failed again. `~/.kube/config` was not used. No API key was used.
