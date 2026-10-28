# Deploy runbook

CD deploys the two Runwatch images to the kind cluster `runwatch`:
namespace `runwatch-dev`, then `runwatch-prod`. The workflow is
`.github/workflows/cd.yaml` (name "CD"); both deploy jobs, and a person by
hand, run the same script, `scripts/deploy.sh <env> <sha>`.

Every `kubectl` command here targets the kind cluster only:
`--kubeconfig ~/.kube/runwatch-kind.config --context kind-runwatch`.
The VM's default kubectl context belongs to another project and is never
used. Cluster setup: [kind-cluster.md](kind-cluster.md).

## The flow

```
push to main -> ci -> build -> deploy-dev -> (approval) -> deploy-prod
```

1. `ci` calls `ci.yaml` (lint, backend, frontend). If it is red nothing builds.
2. `build` (GitHub-hosted) pushes `ghcr.io/coalesce8/runwatch-app:<sha>` and
   `ghcr.io/coalesce8/runwatch-frontend:<sha>`, where `<sha>` is the full
   40-character commit SHA. No `latest` tag.
3. `deploy-dev` (self-hosted runner on the VM, environment `dev`) runs
   `scripts/deploy.sh dev <sha>`.
4. `deploy-prod` (environment `prod`) waits for the reviewer of the `prod`
   environment ("Waiting for review" in the run), then runs
   `scripts/deploy.sh prod <sha>`. It only starts after `deploy-dev` of the
   same SHA succeeded.

`scripts/deploy.sh` checks that the cluster is up and the Secrets exist,
pulls both images and loads them into the kind node (`kind load`, so the
cluster needs no registry credentials and no public package), applies the
manifests with the SHA as image tag, sets the change-cause on the six
Deployments, waits for the rollouts (300 s each, 15 minutes in all),
verifies the image tags and smoke-tests `http://<env>.localhost/` through the
ingress. `deploy.sh dev` also applies `observability/` and creates the
`alertmanager-webhook` Secret when it is absent; `prod` never touches
`runwatch-observability`.

The committed overlays keep `newTag: local`. The script writes the SHA into a
copy of `k8s/` under `.deploy/` (gitignored); nothing tracked changes.

## One-time setup order

1. `make kind-up` (stop Compose first: `docker compose stop`).
2. `make k8s-secrets ENV=dev` and `make k8s-secrets ENV=prod`. They create
   the `postgres` and `agent` Secrets and never overwrite one.
3. The self-hosted runner, then the GitHub environments:
   [self-hosted-runner.md](self-hosted-runner.md).
4. Merge to `main`, or run the workflow by hand (below).

The first deploy of a fresh cluster is slow: the kubelet pulls `postgres:16`,
the OpenTelemetry collector and the observability images from the internet.
If `deploy.sh` times out on `statefulset/postgres` during that first run, wait
until `kubectl -n runwatch-dev get pods` is all `Running` and run it again; it
is idempotent.

## Deploy by hand

```
make deploy ENV=dev                      # images of HEAD
make deploy ENV=prod SHA=<40-hex sha>
DRY_RUN=1 make deploy ENV=dev            # print the plan, contact nothing
SKIP_PULL=1 make deploy ENV=dev          # images built by hand and tagged locally
```

For `SKIP_PULL=1` build and tag the images as
`ghcr.io/coalesce8/runwatch-app:<sha>` and
`ghcr.io/coalesce8/runwatch-frontend:<sha>` first. The run is recorded in the
change-cause as "deploy <first 7 of sha> by manual". Check it with:

```
kubectl --kubeconfig ~/.kube/runwatch-kind.config --context kind-runwatch -n runwatch-dev rollout history deployment/worker
```

## Redeploy a previous SHA (rollback path 2)

Actions > CD > Run workflow, from branch `main`, with `environment` (`dev` or
`prod`) and `sha` (the full 40-character SHA of a commit on `main`; empty
means the commit the run started on).

- A SHA that is not on `main`, or is mistyped, deploys nothing.
- If both images of that SHA are already in GHCR they are not rebuilt;
  otherwise `build` builds them first.
- It redeploys old **images** with the scripts and manifests of the commit the
  run started on (`main`'s head). It does not redeploy old manifests: undo a
  manifest change with a revert PR.
- `prod` waits for the reviewer of the `prod` environment.
- A dispatch from a branch other than `main` skips both deploy jobs.
- The change-cause is "deploy <first 7 of sha> by <run id>".

## A deploy timed out or failed

`deploy.sh` does not roll back by itself. Under `RollingUpdate` the old
ReplicaSet keeps serving while the new pod is not ready. The job log ends with
the `kubectl get pods` output. Then:

1. `kubectl ... -n runwatch-<env> describe pod <pod>` and `logs`.
2. Roll back with [rollback.md](rollback.md) (`make rollback ENV=<env>` or
   redeploy the previous SHA), or fix forward with a new merge.
3. A failed `deploy-dev` stops the push run: `deploy-prod` is skipped.

## First CD run (evidence, 2026-10-28)

Recorded for #35. Read with `gh api` (job times from
`actions/runs/<id>/jobs`, approval from `actions/runs/<id>/approvals` and the
deployment statuses); all times UTC.

- Runner `work-VirtualBox` online, labels `self-hosted,Linux,X64,runwatch-vm`.
  Environment `dev`: no reviewers. Environment `prod`: one required reviewer
  (the repository owner, self-review allowed) and a custom deployment branch
  policy.
- Run [40669522952](https://github.com/coalesce8/ai-dev-tools-zoomcamp-project1/actions/runs/40669522952):
  push of #110 (`9fcb0223068eee855861e3077981209b089c0163`) to `main`.
  `ci` 10:06:56 to 10:18:47 (success); `build` (GitHub-hosted) 10:18:50 to
  10:19:11; `deploy-dev` 11:03:54 to 11:07:39 (about 4 minutes, runner
  `work-VirtualBox`); `deploy-prod` 11:14:35 to 11:15:44 (success).
- `deploy-dev` sat queued from 10:19 to 11:03: the runner had been registered
  only at about 10:53, and it first ran the older push run 40601364357
  (`919d9c1`, deploy-dev 10:53:35 to 11:03:51, deploy-prod 11:11:16 to
  11:12:16).
- Prod approval: `coalesce8` approved the `prod` deployment; the approvals API
  gives no time, the deployment statuses do: `waiting` 11:12:17, approved
  (`queued`) 11:14:33, `in_progress` 11:14:35. `deploy-prod` did nothing
  while waiting (it waited behind the `deploy-prod` concurrency group for the
  older run until 11:12:16, then for the reviewer for about 2 minutes). For
  `919d9c1`: waiting 11:03:53, approved 11:11:15.
- Job log of both deploy jobs: `== images:
  ghcr.io/coalesce8/runwatch-app:9fcb0223... ghcr.io/coalesce8/runwatch-frontend:9fcb0223...`,
  `6 container image(s) end with :9fcb0223068eee855861e3077981209b089c0163`,
  smoke checks passed.
- Cross-check afterwards (read-only): all six Deployments of `runwatch-dev`
  and `runwatch-prod` run the `:9fcb0223...` app and frontend images;
  `rollout history deployment/api` newest revision is `deploy 9fcb022 by
  40669522952` in both (dev revision 7, prod revision 4);
  `http://dev.localhost/` and `http://prod.localhost/` return 200.
- `gh run list --workflow CD --event pull_request` is empty.
- Run 40676273591 (`workflow_dispatch` on `main`, 10:58:12 to 10:58:43, empty
  `sha`): only `build` ran. `ci` runs only on `push`, so it is skipped on a
  dispatch; `build` found no `sha` input, chose `main`'s head and rebuilt and pushed that SHA's images again (the
  "both tags exist" skip applies only when `sha` is set); both
  deploy jobs were skipped. It deployed nothing and is not counted as evidence
  for the dispatch criteria.

Not yet exercised: a dispatch with `environment=dev` and the previous `sha`
(rollback path 2), and a dispatch from a branch other than `main` (both
deploy jobs must skip).

## After a rollback: the next merge deploys the committed state

After an agent or manual `kubectl rollout undo` the cluster differs from
`main`. CD deploys the committed manifests and the images of the new commit as
they are, so the next merge to `main` rolls the bad release out again. Open
the revert PR first (`git revert <bad commit>`) and merge it before anything
else, as the incident report says.

## Rotating a webhook token

The Secret `alertmanager-webhook` is built once from the `webhook-token` of
the two `agent` Secrets and is not touched again by later deploys. To rotate:
replace the token in the `agent` Secret of the environment, rebuild the
`alertmanager-webhook` Secret, then restart both sides.

```
k="kubectl --kubeconfig $HOME/.kube/runwatch-kind.config --context kind-runwatch"
$k -n runwatch-observability create secret generic alertmanager-webhook \
  --from-literal "dev-token=$($k -n runwatch-dev get secret agent -o jsonpath='{.data.webhook-token}' | base64 -d)" \
  --from-literal "prod-token=$($k -n runwatch-prod get secret agent -o jsonpath='{.data.webhook-token}' | base64 -d)" \
  --dry-run=client -o yaml | $k apply -f -
$k -n runwatch-observability rollout restart deployment/alertmanager
$k -n runwatch-dev rollout restart deployment/agent
$k -n runwatch-prod rollout restart deployment/agent
```

## Notes

- A second deploy of the same SHA creates no new ReplicaSet. `kubectl apply`
  reports `statefulset.apps/postgres configured` each time; that is API-server
  defaulting and changes nothing.
- Telemetry and logs never carry a Secret value; the scripts do not `set -x`.
- Cleaning old images from the VM disk and the kind node is out of scope (#74).
