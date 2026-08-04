#!/usr/bin/env bash
# Move twenty-postgres-data from pi51 to server-1.
# Read docs/pi-decommission.md before running any of this.
#
#   ./scripts/twenty-db-migrate.sh backup       # stream the pi51 volume to $BACKUP_DIR
#   ./scripts/twenty-db-migrate.sh delete-pvc   # DESTRUCTIVE: drops the pi51 data
#   ./scripts/twenty-db-migrate.sh restore      # provision on server-1 and restore
#   ./scripts/twenty-db-migrate.sh verify       # show where the PV now lives
set -euo pipefail

NS=twenty
SRC_NODE=pi51
DST_NODE=server-1
PVC=twenty-postgres-data
BACKUP_DIR="${BACKUP_DIR:-/tmp/twenty-migration}"
HELPER_IMAGE="${HELPER_IMAGE:-busybox:1.38.0}"

log() { printf '%s %s\n' "$(date -Iseconds)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

pod_manifest() {
  local name=$1 node=$2 readonly=$3
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
        - name: data
          mountPath: /data
          readOnly: ${readonly}
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: ${PVC}
YAML
}

wait_running() {
  local pod=$1 deadline=$((SECONDS + 900))
  log "waiting for pod/${pod} to run"
  while [ "$(kubectl get pod -n "$NS" "$pod" -o jsonpath='{.status.phase}' 2>/dev/null)" != "Running" ]; do
    [ $SECONDS -gt $deadline ] && die "pod/${pod} did not start in time"
    sleep 5
  done
}

# Postgres must not be running while its PGDATA is copied, and the app must not
# be running or it will keep reconnecting and writing.
assert_scaled_down() {
  for d in twenty twenty-db; do
    local n
    n=$(kubectl get deploy -n "$NS" "$d" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo missing)
    [ "$n" = "0" ] || die "deployment/${d} has replicas=${n}, expected 0. Push commit A first."
  done
  [ "$(kubectl get pods -n "$NS" -l app.kubernetes.io/name=twenty-db --no-headers 2>/dev/null | wc -l)" = "0" ] \
    || die "a twenty-db pod is still terminating — wait for it to go"
  log "twenty and twenty-db are scaled to 0"
}

cmd_backup() {
  assert_scaled_down
  mkdir -p "$BACKUP_DIR"
  local pod=twenty-db-backup
  kubectl delete pod -n "$NS" "$pod" --ignore-not-found --wait=true >/dev/null
  pod_manifest "$pod" "$SRC_NODE" true | kubectl apply -f - >/dev/null
  wait_running "$pod"

  log "backing up ${PVC} from ${SRC_NODE}"
  # -C into the mount so the archive has no leading path component
  kubectl exec -n "$NS" "$pod" -- tar czf - -C /data . > "${BACKUP_DIR}/${PVC}.tgz"
  local size
  size=$(stat -c %s "${BACKUP_DIR}/${PVC}.tgz")
  [ "$size" -gt 1048576 ] || die "${PVC}.tgz is only ${size} bytes — refusing to continue"
  log "${PVC} -> ${BACKUP_DIR}/${PVC}.tgz ($(numfmt --to=iec "$size"))"

  # PG_VERSION is the cheapest proof the archive holds a real PGDATA
  tar tzf "${BACKUP_DIR}/${PVC}.tgz" | grep -q './pgdata/PG_VERSION' \
    || die "archive has no pgdata/PG_VERSION — refusing to continue"
  log "archive contains pgdata/PG_VERSION"

  kubectl delete pod -n "$NS" "$pod" --wait=false >/dev/null
  log "backup complete. Verify the archive before running delete-pvc."
}

cmd_delete_pvc() {
  assert_scaled_down
  [ -s "${BACKUP_DIR}/${PVC}.tgz" ] || die "missing or empty ${BACKUP_DIR}/${PVC}.tgz — run backup first"

  cat >&2 <<EOF

  About to delete PVC ${NS}/${PVC}.

  The local-path StorageClass reclaim policy is Delete, so this REMOVES the
  data on ${SRC_NODE}. ${BACKUP_DIR}/${PVC}.tgz becomes the only copy
  (plus the pg_dumpall taken before the migration started).

EOF
  read -r -p "  Type the word DELETE to continue: " answer
  [ "$answer" = "DELETE" ] || die "aborted"

  kubectl delete pvc -n "$NS" "$PVC" --wait=true
  log "PVC deleted. Argo CD recreates it; it stays Pending until a consumer schedules."
}

cmd_restore() {
  assert_scaled_down
  local pod=twenty-db-restore deadline=$((SECONDS + 300))

  log "waiting for Argo CD to recreate the PVC"
  while ! kubectl get pvc -n "$NS" "$PVC" >/dev/null 2>&1; do
    [ $SECONDS -gt $deadline ] && die "pvc/${PVC} was not recreated — check the twenty Application"
    sleep 5
  done

  kubectl delete pod -n "$NS" "$pod" --ignore-not-found --wait=true >/dev/null
  # Scheduling this pod on $DST_NODE is what makes local-path provision there
  pod_manifest "$pod" "$DST_NODE" false | kubectl apply -f - >/dev/null
  wait_running "$pod"

  log "restoring ${PVC} onto ${DST_NODE}"
  kubectl exec -i -n "$NS" "$pod" -- tar xzf - -C /data < "${BACKUP_DIR}/${PVC}.tgz"
  kubectl exec -n "$NS" "$pod" -- test -f /data/pgdata/PG_VERSION \
    || die "pgdata/PG_VERSION missing after restore"
  # postgres refuses to start unless PGDATA is 0700 and owned by uid 999.
  # 999:0 is what the postgres:16 entrypoint leaves behind — match it exactly.
  kubectl exec -n "$NS" "$pod" -- sh -c 'chown -R 999:0 /data/pgdata && chmod 700 /data/pgdata'
  log "restored ($(kubectl exec -n "$NS" "$pod" -- sh -c 'ls -A /data/pgdata | wc -l') entries in pgdata)"

  kubectl delete pod -n "$NS" "$pod" --wait=false >/dev/null
  cmd_verify
  log "restore complete. Push commit B to scale twenty-db and twenty back to 1."
}

cmd_verify() {
  kubectl get pv -o json | jq -r --arg ns "$NS" --arg pvc "$PVC" '.items[]
    | select(.spec.claimRef.namespace==$ns and .spec.claimRef.name==$pvc)
    | "\(.spec.claimRef.name) -> \(.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]) [\(.status.phase)]"'
}

case "${1:-}" in
  backup)     cmd_backup ;;
  delete-pvc) cmd_delete_pvc ;;
  restore)    cmd_restore ;;
  verify)     cmd_verify ;;
  *) die "usage: $0 {backup|delete-pvc|restore|verify}" ;;
esac
