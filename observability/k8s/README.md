# runwatch-observability on kind

One shared Prometheus, Tempo, Loki, Grafana and Alertmanager for the `dev`
and `prod` environments. The Kustomization is `observability/kustomization.yaml`
(its root is `observability/` so it reuses the config files Compose uses).
Compose and kind do not run at the same time (the VM cannot hold both).

## Apply

    kubectl apply -k observability

Alertmanager stays in `CreateContainerConfigError` until the Secret below exists.

## Alertmanager token Secret (once, not in the repo)

Run after `scripts/k8s-secrets.sh` for both environments. It copies each
environment's agent token; the values are never printed.

    kubectl -n runwatch-observability create secret generic alertmanager-webhook \
      --from-literal=dev-token="$(kubectl -n runwatch-dev get secret agent -o jsonpath='{.data.webhook-token}' | base64 -d)" \
      --from-literal=prod-token="$(kubectl -n runwatch-prod get secret agent -o jsonpath='{.data.webhook-token}' | base64 -d)"

Rotating an agent's token means re-creating this Secret and restarting
Alertmanager: `kubectl -n runwatch-observability rollout restart deploy/alertmanager`.

## Reach the UIs

Nothing is routed from outside the cluster (Grafana allows anonymous Admin).

    kubectl -n runwatch-observability port-forward svc/grafana 3001:3000
    kubectl -n runwatch-observability port-forward svc/prometheus 9091:9090

## Notes

- Alerts route by `env` to `agent.runwatch-dev` / `agent.runwatch-prod` port
  8090, path `/webhook/alertmanager` (`k8s/alertmanager.yaml`). There is no
  `local` route and no `local` rule on kind.
- Data lives in `emptyDir` volumes: telemetry is lost when a pod restarts.
- Memory limits total 1088Mi (prometheus, tempo, loki, grafana 256Mi each,
  alertmanager 64Mi); requests total 576Mi. The Go components set
  `GOMEMLIMIT` to about 80% of the limit.
