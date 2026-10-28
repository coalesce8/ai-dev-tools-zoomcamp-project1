# Rollback runbook

Every `kubectl` command here targets the kind cluster only:
`--kubeconfig ~/.kube/runwatch-kind.config --context kind-runwatch -n runwatch-<env>`.
The VM's default kubectl context is an unrelated cluster and must not be
used. Cluster setup: [kind-cluster.md](kind-cluster.md).

## When to roll back

A deploy made things worse: `JobFailureRateHigh` fires, or Grafana shows
errors for the `env` right after a deploy. Roll back first, diagnose after.

## Path 1 - `make rollback ENV=<env>`

Runs `kubectl rollout undo` for `api worker frontend loadgen agent
otel-collector`, in that order, waiting for each with `rollout status`.

- Undoes: the Pod template of those six Deployments (image, env, config)
- Does not undo: the database and Alembic migrations (a rollback is safe only
  when the bad release's migrations are backward compatible), Secrets, the
  observability namespace and the `postgres` StatefulSet
- Options: `DEPLOY=<name>` limits it to one of the six; `REVISION=<n>` (only
  with `DEPLOY`) adds `--to-revision=<n>`
- `ENV=prod` needs `CONFIRM=yes`; a missing or other `ENV` exits 2
- A Deployment with no previous revision is reported as skipped; other failures stop the script
- `DRY_RUN=1` prints the commands without running them

Running `rollout undo` twice undoes the undo. Read the history first:

```
kubectl --kubeconfig ~/.kube/runwatch-kind.config --context kind-runwatch -n runwatch-dev rollout history deployment/worker
```

## Path 2 - redeploy a previous SHA

1. Find the last good SHA: the commit before the bad merge on `main`, or the
   commit of the last green deploy run in the Actions tab. Copy its full
   40-character SHA.
2. GitHub, Actions tab, workflow "CD" (the deploy workflow from #35), `Run
   workflow`, from branch `main`. A dispatch from another branch deploys
   nothing.
3. Inputs: `environment` (`dev` or `prod`) and `sha`, the full 40-character
   SHA of a commit on `main` (the run fails for any other commit; an empty
   `sha` deploys the commit the run started on). If either of the two images
   is missing in GHCR for that SHA, they are built first, so an old SHA whose
   images are gone still works. The change-cause is set to
   `deploy <first 7 of sha> by <run id>`.
4. For `prod`, approve the run in the `prod` environment. A dispatch to
   `prod` needs no earlier dev run, only that approval.

The old images are deployed with the CURRENT manifests and scripts of
`main`'s head, not the old manifests. A manifest change is rolled back with a
revert PR (see "After a rollback").

The self-hosted runner must be online (#73, see
[self-hosted-runner.md](self-hosted-runner.md)), otherwise the run queues.

## Which path to use

Path 1 takes seconds and needs the VM. Path 2 is the durable one: it moves
the cluster to a known commit and needs no kubectl.

## After a rollback

The cluster now differs from `main`. The committed overlays hold
`newTag: local` and CD sets the SHA at deploy time, so there is no tag line to
revert. Open a PR on `main` with `git revert <bad commit>` and merge it so CD
redeploys a good tree. Do not let any other merge land first, or CD rolls the
bad release out again.

## Verify

```
kubectl --kubeconfig ~/.kube/runwatch-kind.config --context kind-runwatch -n runwatch-dev rollout status deployment/worker
```

The alert resolves, and a new job reaches `SUCCEEDED` in the UI.

## The agent's own undo

The responder agent has `k8s_rollout_undo` for the worker only (#27), and its
incident report carries the revert note (#29). A human uses the same two
paths. The agent has no other rollback logic.

## Drill scripts

All act on `runwatch-dev`, take no `ENV`, and honour `DRY_RUN=1`.

| Command | What it does | Alerts to expect |
| --- | --- | --- |
| `make break-worker` | scales the worker to 0 | `JobsStalled` (2 min), `QueueWaitHigh` (4 min) |
| `make bad-release` | annotates change-cause, sets `WORKER_FAULT=fail_all` (new revision) | `JobFailureRateHigh` (5 min) |
| `make heal` | clears `WORKER_FAULT`, scales the worker to 1, waits for rollout | none; alerts clear on their own |

`heal` is safe in any state. It does not requeue jobs: use the UI or
`POST /internal/v1/jobs/requeue-failed` with `{"since", "until"}`, which is
not routed through the ingress (404 there): reach it with `kubectl port-forward
svc/api -n runwatch-dev 18000:8000`. Stalled jobs can be requeued one by one
with the public `POST /api/v1/jobs/{id}/requeue`.

## Exercised rollbacks

- 2026-10-28, path 1: `make rollback ENV=dev DEPLOY=worker` undid the manual
  `bad-release` revision of the worker in `runwatch-dev` on the kind cluster;
  `JobFailureRateHigh` resolved 3 m 47 s after the command. Command output and
  timings: row 10 of [../live-checks.md](../live-checks.md); incident:
  [../../incidents/README.md](../../incidents/README.md).
- The agent's own `k8s_rollout_undo`: not yet exercised with a live model, owner
  step. See "Owner-only" in [../../incidents/README.md](../../incidents/README.md).
- The redeploy of a previous SHA (path 2): not yet exercised through CD, owner
  step. See "Owner-only" in [../../incidents/README.md](../../incidents/README.md).
