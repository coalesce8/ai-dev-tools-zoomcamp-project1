# kind cluster runbook

One single-node kind cluster, `runwatch`, hosts the dev and prod
deployments and the observability stack. This issue creates the empty
cluster: namespaces `runwatch-dev`, `runwatch-prod`,
`runwatch-observability`, and an ingress-nginx controller. App workloads
arrive later.

## Tools

`kind` (v0.30.0) and `kubectl` (1.37, includes `kubectl kustomize`) live
in `~/.local/bin`. No root, no system packages, no standalone `kustomize`
or `helm`. If one is missing, download the release binary into
`~/.local/bin` (`kind` from the `kubernetes-sigs/kind` GitHub release,
`kubectl` from `dl.k8s.io`) and verify its checksum.

kubectl 1.37 against server 1.34 is outside the usual one-minor skew but
works for these commands. Use the `kubectl` from the same install.

## Targets

- `make kind-up` - create the cluster, or `docker start` the stopped one;
  re-apply `deploy/kind` (`kubectl apply -k`); wait for the node
  (120 s) and the ingress controller (180 s). Safe to run repeatedly.
- `make kind-down` - `docker stop runwatch-control-plane`. Nothing is
  deleted. Exits 0 if the container is already stopped or absent.
- `make kind-load IMAGES="a:tag b:tag"` - `kind load docker-image` into
  the cluster. Fails with a usage message if `IMAGES` is empty or the
  cluster is not running.

Variables: `KIND_KUBECONFIG` (default `~/.kube/runwatch-kind.config`),
`KIND_MIN_AVAILABLE_MB` (default 1500). The script also reads
`KIND_MEMORY` (default `3g`). They are not read by the app.

Re-running `make kind-up` creates no second cluster. `kubectl apply -k`
reports `unchanged` for almost everything; the ingress-nginx admission
Jobs have `ttlSecondsAfterFinished: 0`, so they are re-created on every
apply, and the Deployment and the admission webhook show `configured`
(a client-side apply quirk of the upstream manifest, no real change).
`make kind-load` first tries `kind load docker-image`; if that fails on a
multi-platform image (Docker's containerd image store), it retries with a
single-platform archive.

## Dedicated kubeconfig

The cluster's kubeconfig is `~/.kube/runwatch-kind.config`, context
`kind-runwatch`. `~/.kube/config` is never modified: its current context
belongs to another project's cluster (`agent-relay`). Always pass both:

    kubectl --kubeconfig ~/.kube/runwatch-kind.config --context kind-runwatch get nodes

or set `KUBECONFIG=$HOME/.kube/runwatch-kind.config` and pass `--context`.
The API server port is random and may change after a stop/start;
`make kind-up` re-exports the kubeconfig.

## Memory and the start threshold

The node container is capped at 3 GB (`docker update --memory 3g
--memory-swap 3g`), which enforces "the whole cluster fits in 3 GB".
`make kind-up` refuses to create or start the cluster when `MemAvailable`
is below 1500 MB: the VM has 5.3 GB and already swaps. It prints the
number it saw and exits non-zero.

## Compose and kind never run together

Stop Compose first with `docker compose stop` (never `down`, never `-v`).
`make kind-up` refuses to run while a Runwatch Compose service is up.

## Hostnames and port 80

The node maps container port 80 to `127.0.0.1:80` (loopback only, no
TLS, no 443). `dev.localhost` and `prod.localhost` reach the ingress
controller. Until routes exist it answers 404:

    curl -s -o /dev/null -w '%{http_code}' -H 'Host: dev.localhost' http://127.0.0.1/

If the VM's resolver does not map `*.localhost`, use the `Host` header
form above. If something already listens on port 80, `make kind-up`
stops and reports; it does not choose another port. Ingress resources
use `ingressClassName: nginx`.

## Loading images

There is no local registry: one fewer container on a tight VM, and `kind
load` is enough because the only local images are the two Runwatch
images (`runwatch-app`, `runwatch-frontend`):

    make kind-load IMAGES="runwatch-app:dev runwatch-frontend:dev"

CD pulls SHA-tagged images from GHCR instead (public, no credentials).

## ingress-nginx

`deploy/kind/ingress-nginx.yaml` is the upstream kind provider manifest
vendored at `controller-v1.15.1` (tag in its first line), applied through
`deploy/kind/kustomization.yaml` with a controller memory request of
100Mi and limit of 200Mi. Upgrade by downloading a newer pinned tag into
that file; never apply a moving URL.

## Reset (run by the user, not by agents)

Agents never delete the cluster. To start from scratch, the user runs:

    kind delete cluster --name runwatch

and, only if wanted, `rm -f ~/.kube/runwatch-kind.config`. Then
`make kind-up` creates a fresh cluster.

## Troubleshooting

- `too many open files` or inotify errors when two kind clusters run:
  raise `fs.inotify.max_user_instances` (default 128). This needs root,
  so it is the user's to do.
- The node is not `Ready` after a stop/start: report it. Do not delete
  the cluster; check `docker logs runwatch-control-plane` and
  `kubectl ... describe node`.
