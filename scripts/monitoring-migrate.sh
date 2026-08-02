#!/usr/bin/env bash
# Move the monitoring stack's local-path volumes from pi52 to server-1.
# Read docs/monitoring-migration.md before running any of this.
#
#   ./scripts/monitoring-migrate.sh backup       # stream pi52 volumes to $BACKUP_DIR
#   ./scripts/monitoring-migrate.sh delete-pvcs  # DESTRUCTIVE: drops the pi52 data
#   ./scripts/monitoring-migrate.sh restore      # provision on server-1 and restore
#   ./scripts/monitoring-migrate.sh verify       # show where each PV now lives
set -euo pipefail

NS=monitoring
SRC_NODE=pi52
DST_NODE=server-1
PVCS=(grafana-data prometheus-data loki-data)
BACKUP_DIR="${BACKUP_DIR:-/tmp/monitoring-migration}"
HELPER_IMAGE="${HELPER_IMAGE:-busybox:1.38.0}"

log() { printf '%s %s\n' "$(date -Iseconds)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# Emit the volumes/volumeMounts stanzas for every PVC in one helper pod.
pod_manifest() {
  local name=$1 node=$2 readonly=$3
  {
    cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${name}
  namespace: ${NS}
spec:
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/hostname: ${node}
  tolerations:
    - operator: Exists
  containers:
    - name: helper
      image: ${HELPER_IMAGE}
      command: ["sh", "-c", "sleep 3600"]
      volumeMounts:
YAML
    for p in "${PVCS[@]}"; do
      printf '        - name: %s\n          mountPath: /data/%s\n          readOnly: %s\n' "$p" "$p" "$readonly"
    done
    printf '  volumes:\n'
    for p in "${PVCS[@]}"; do
      printf '    - name: %s\n      persistentVolumeClaim:\n        claimName: %s\n' "$p" "$p"
    done
  }
}

wait_running() {
  local pod=$1 deadline=$((SECONDS + 900))
  log "waiting for pod/${pod} to run (this is slow on ${SRC_NODE})"
  while [ "$(kubectl get pod -n "$NS" "$pod" -o jsonpath='{.status.phase}' 2>/dev/null)" != "Running" ]; do
    [ $SECONDS -gt $deadline ] && die "pod/${pod} did not start in time"
    sleep 5
  done
}

assert_scaled_down() {
  for d in grafana prometheus loki; do
    local n
    n=$(kubectl get deploy -n "$NS" "$d" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo missing)
    [ "$n" = "0" ] || die "deployment/${d} has replicas=${n}, expected 0. Push commit A first."
  done
  log "all three deployments are scaled to 0"
}

cmd_backup() {
  assert_scaled_down
  mkdir -p "$BACKUP_DIR"
  local pod=migrate-backup
  kubectl delete pod -n "$NS" "$pod" --ignore-not-found --wait=true >/dev/null
  pod_manifest "$pod" "$SRC_NODE" true | kubectl apply -f - >/dev/null
  wait_running "$pod"

  for p in "${PVCS[@]}"; do
    log "backing up ${p}"
    # -C into the mount so the archive has no leading path component
    kubectl exec -n "$NS" "$pod" -- tar czf - -C "/data/${p}" . > "${BACKUP_DIR}/${p}.tgz"
    local size
    size=$(stat -c %s "${BACKUP_DIR}/${p}.tgz")
    [ "$size" -gt 1024 ] || die "${p}.tgz is only ${size} bytes — refusing to continue"
    log "${p} -> ${BACKUP_DIR}/${p}.tgz ($(numfmt --to=iec "$size"))"
  done

  kubectl delete pod -n "$NS" "$pod" --wait=false >/dev/null
  log "backup complete. Verify the archives before running delete-pvcs."
}

cmd_delete_pvcs() {
  assert_scaled_down
  for p in "${PVCS[@]}"; do
    [ -s "${BACKUP_DIR}/${p}.tgz" ] || die "missing or empty ${BACKUP_DIR}/${p}.tgz — run backup first"
  done
  log "archives present for all ${#PVCS[@]} volumes"

  cat >&2 <<EOF

  About to delete these PVCs in namespace ${NS}:
      ${PVCS[*]}

  The local-path StorageClass reclaim policy is Delete, so this REMOVES the
  data on ${SRC_NODE}. The archives in ${BACKUP_DIR} become the only copy.

EOF
  read -r -p "  Type the word DELETE to continue: " answer
  [ "$answer" = "DELETE" ] || die "aborted"

  for p in "${PVCS[@]}"; do
    log "deleting pvc/${p}"
    kubectl delete pvc -n "$NS" "$p" --wait=true
  done
  log "PVCs deleted. Argo CD will recreate them; they stay Pending until a consumer is scheduled."
}

cmd_restore() {
  assert_scaled_down
  local pod=migrate-restore deadline=$((SECONDS + 300))

  # Wait for Argo CD to have recreated all three PVCs
  log "waiting for Argo CD to recreate the PVCs"
  for p in "${PVCS[@]}"; do
    while ! kubectl get pvc -n "$NS" "$p" >/dev/null 2>&1; do
      [ $SECONDS -gt $deadline ] && die "pvc/${p} was not recreated — check the monitoring Application"
      sleep 5
    done
  done

  kubectl delete pod -n "$NS" "$pod" --ignore-not-found --wait=true >/dev/null
  # Scheduling this pod on $DST_NODE is what makes local-path provision there
  pod_manifest "$pod" "$DST_NODE" false | kubectl apply -f - >/dev/null
  wait_running "$pod"

  for p in "${PVCS[@]}"; do
    log "restoring ${p}"
    kubectl exec -i -n "$NS" "$pod" -- tar xzf - -C "/data/${p}" < "${BACKUP_DIR}/${p}.tgz"
    local n
    n=$(kubectl exec -n "$NS" "$pod" -- sh -c "ls -A /data/${p} | wc -l")
    [ "$n" -gt 0 ] || die "${p} is empty after restore"
    log "${p} restored (${n} entries at top level)"
  done

  kubectl delete pod -n "$NS" "$pod" --wait=false >/dev/null
  cmd_verify
  log "restore complete. Push commit B to scale the deployments back to 1."
}

cmd_verify() {
  kubectl get pv -o json | jq -r --arg ns "$NS" '.items[]
    | select(.spec.claimRef.namespace==$ns)
    | "\(.spec.claimRef.name) -> \(.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]) [\(.status.phase)]"' \
    | sort
}

case "${1:-}" in
  backup)      cmd_backup ;;
  delete-pvcs) cmd_delete_pvcs ;;
  restore)     cmd_restore ;;
  verify)      cmd_verify ;;
  *) die "usage: $0 {backup|delete-pvcs|restore|verify}" ;;
esac
