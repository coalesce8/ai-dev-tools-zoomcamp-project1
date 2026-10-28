# Self-hosted runner and GitHub environments

Steps for the repository owner. The deploy jobs of
`.github/workflows/cd.yaml` run on a runner on the VM, because only the VM
reaches the kind cluster. Nothing here needs `sudo` and nothing goes into the
repository. Done by the owner on 2026-10-28 (#73, closed); kept for
re-registering the runner.

Register the runner **after** `cd.yaml` is on `main`.

## 1. Prerequisites on the VM

The user that runs the runner needs `docker` (member of the `docker` group),
`kind` and `kubectl` on its `PATH` (`~/.local/bin`), and read access to
`~/.kube/runwatch-kind.config`. Set up the cluster and the Secrets first, see
[deploy.md](deploy.md) (`make kind-up`, `make k8s-secrets ENV=dev`,
`ENV=prod`).

## 2. Install and register the runner

As the VM user, in `~/actions-runner`:

1. Settings > Actions > Runners > New self-hosted runner > Linux, x64. Copy the
   download commands and the one-time registration token from that page.
   Never write the token to a file in the repository.
2. Download and unpack the runner release into `~/actions-runner` (the
   commands on that page; check the checksum it shows).
3. Register it to this repository with the label `runwatch-vm`:

```
cd ~/actions-runner
./config.sh --url https://github.com/coalesce8/ai-dev-tools-zoomcamp-project1 \
  --labels runwatch-vm --unattended --token <registration token>
```

The runner also carries the default labels `self-hosted`, `linux`, `x64`; the
deploy jobs ask for `[self-hosted, runwatch-vm]`.

## 3. Run it

In the foreground:

```
cd ~/actions-runner && ./run.sh
```

or as a `systemd --user` unit (no sudo), for example
`~/.config/systemd/user/actions-runner.service`:

```
[Unit]
Description=GitHub Actions runner (runwatch)

[Service]
WorkingDirectory=%h/actions-runner
ExecStart=%h/actions-runner/run.sh
Restart=on-failure
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
```

then `systemctl --user daemon-reload && systemctl --user enable --now actions-runner`.
Do not use `./svc.sh install`: it needs sudo.

Check that it is online:

```
gh api repos/coalesce8/ai-dev-tools-zoomcamp-project1/actions/runners --jq '.runners[] | {name, status, labels: [.labels[].name]}'
```

Stop it with `Ctrl-C`, or `systemctl --user stop actions-runner`. It may be
stopped whenever nobody is deploying: queued deploy jobs wait for it.

## 4. Security

- The deploy jobs run only for a push to `main` or a manual dispatch from
  `main`: `cd.yaml` has no `pull_request`, `pull_request_target` or
  `workflow_run` trigger, and each deploy job checks `github.ref ==
  'refs/heads/main'`.
- A self-hosted runner on a public repository must never be given
  `pull_request` workflows: code from a pull request would run on the VM.
- Settings > Actions > General > "Fork pull request workflows from outside
  collaborators" > "Require approval for all outside collaborators", so
  outside code never runs without approval (tracked by #71).
- The workflow uses only the built-in `GITHUB_TOKEN`. The runner logs in to
  `ghcr.io` with it into a temporary Docker config for the job.

## 5. Create the environments

`dev` (no reviewers), and `prod` with the owner as required reviewer and a
deployment branch policy of `main` only. One account does all the work, so
self-review stays allowed.

```
repo=coalesce8/ai-dev-tools-zoomcamp-project1
gh api -X PUT repos/$repo/environments/dev

uid=$(gh api user --jq .id)
gh api -X PUT repos/$repo/environments/prod --input - <<JSON
{
  "reviewers": [{"type": "User", "id": $uid}],
  "prevent_self_review": false,
  "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}
}
JSON
gh api -X POST repos/$repo/environments/prod/deployment-branch-policies -f name=main -f type=branch
```

Check: `gh api repos/$repo/environments --jq '.environments[].name'` lists
`dev` and `prod`.

## 6. First run

Merge a docs-only PR to `main` (or run Actions > CD > Run workflow with
`environment=dev`). `ci`, `build` and `deploy-dev` pass; `deploy-prod` shows
"Waiting for review" until the reviewer approves it. Then
`http://dev.localhost/` and `http://prod.localhost/` answer 200 on the VM.
`gh run list --workflow CD --event pull_request` stays empty.
