# Shared helpers for the drill and rollback scripts. Sourced, not executed.
#
# Every kubectl call goes through kc(), which pins the kubeconfig, context and
# namespace. The VM's default kubectl context is an unrelated cluster and is
# never used. DRY_RUN=1 prints each command prefixed "DRY_RUN: " and runs nothing.

KIND_KUBECONFIG="${KIND_KUBECONFIG:-$HOME/.kube/runwatch-kind.config}"
KIND_CONTEXT="kind-runwatch"

# drill_init <env>: set the namespace and, unless DRY_RUN=1, check the cluster config exists.
drill_init() {
  NS="runwatch-$1"
  KC_BASE=(--kubeconfig "$KIND_KUBECONFIG" --context "$KIND_CONTEXT" -n "$NS")
  if [ "${DRY_RUN:-}" != "1" ] && [ ! -f "$KIND_KUBECONFIG" ]; then
    echo "$KIND_KUBECONFIG not found: run make kind-up first" >&2
    exit 1
  fi
}

# drills_dev_only: the drills act on runwatch-dev; refuse an ENV that says otherwise.
drills_dev_only() {
  if [ -n "${ENV:-}" ] && [ "$ENV" != "dev" ]; then
    echo "this drill acts on runwatch-dev only; ENV=$ENV is not accepted" >&2
    exit 2
  fi
  drill_init dev
}

# kc <kubectl args...>: the only place kubectl is called.
kc() {
  if [ "${DRY_RUN:-}" = "1" ]; then
    echo "DRY_RUN: kubectl ${KC_BASE[*]} $*"
    return 0
  fi
  kubectl "${KC_BASE[@]}" "$@"
}

# kc_read <kubectl args...>: a read whose output the script uses; empty in DRY_RUN.
kc_read() {
  if [ "${DRY_RUN:-}" = "1" ]; then
    echo "DRY_RUN: kubectl ${KC_BASE[*]} $*" >&2
    return 0
  fi
  kubectl "${KC_BASE[@]}" "$@"
}
