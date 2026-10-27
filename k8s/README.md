# Kubernetes manifests

Kustomize base plus two overlays: `k8s/base/` (all components),
`k8s/overlays/dev` and `k8s/overlays/prod` (namespace `runwatch-dev` /
`runwatch-prod`, image tags, `DEPLOYMENT_ENV` / `AGENT_ENV`, ingress host,
`env` label). Render with `make k8s-render ENV=dev`, validate offline with
`make k8s-validate`.

## Image tags

Both overlays set `images:` with one `newTag:` line per image
(`ghcr.io/coalesce8/runwatch-app`, `ghcr.io/coalesce8/runwatch-frontend`).
The committed value is `local`, for images loaded into kind by hand (#33).
CD (#35) replaces it with the commit SHA (`sed` or `kustomize edit set image`).

## Secrets

Not in the repo. `make k8s-secrets ENV=dev|prod` (`scripts/k8s-secrets.sh`)
creates them with random values and never overwrites one:

- `postgres`: `password`
- `agent`: `webhook-token`, and `anthropic-api-key` / `github-token` when
  `ANTHROPIC_API_KEY` / `GITHUB_TOKEN` are set (both optional for the agent)

## Contract with `runwatch-observability` (#32)

Services called from here: `prometheus` 9090, `loki` 3100, `tempo` 4318.
Services #32 provides for others: `alertmanager` 9093, `grafana` 3000.

## Ingress

`ingressClassName: nginx` needs an ingress controller serving that class
(#33). Hosts: `dev.localhost`, `prod.localhost`; `/api/v1` goes to the API,
`/` to the frontend.

## Memory budget (limits, per environment)

| Container | Limit |
| --- | --- |
| postgres | 192Mi |
| api (migrate init container too) | 192Mi |
| worker | 96Mi |
| loadgen | 96Mi |
| frontend | 48Mi |
| otel-collector | 96Mi |
| agent | 128Mi |
| Total | 848Mi (ceiling 900Mi) |
