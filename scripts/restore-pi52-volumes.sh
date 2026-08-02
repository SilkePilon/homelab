#!/usr/bin/env bash
# Restore volumes recovered from pi52's SD card onto server-1.
#
# pi52's card stopped completing writes (100% io utilisation at 0.76 MB/s).
# Reads were still healthy at ~77 MB/s, so the data was copied off intact
# rather than imaged. See docs/pi52-recovery.md.
#
#   ./scripts/restore-pi52-volumes.sh check    # verify the source data
#   ./scripts/restore-pi52-volumes.sh delete   # DESTRUCTIVE: drop the dead PVCs
#   ./scripts/restore-pi52-volumes.sh restore  # provision on server-1 and fill
#   ./scripts/restore-pi52-volumes.sh verify   # show where each PV now lives
set -euo pipefail

SRC="${SRC:-$HOME/pi52-recovery}"
DST_NODE=server-1
HELPER_IMAGE="${HELPER_IMAGE:-busybox:1.38.0}"
HELPER_NS=node-maintenance
HELPER_POD=pi52-restore

# namespace:pvc — the six volumes that lived on pi52
VOLUMES=(
  monitoring:grafana-data
  monitoring:prometheus-data
  monitoring:loki-data
  homeassistant:homeassistant-config
  n8n:n8n-data
  twenty:twenty-server-data
)

log() { printf '%s %s\n' "$(date -Iseconds)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# The recovery directories are named pvc-<uuid>_<namespace>_<pvc>
src_dir_for() {
  local ns=$1 pvc=$2 d
  d=$(find "$SRC" -maxdepth 1 -type d -name "pvc-*_${ns}_${pvc}" | head -1)
  [ -n "$d" ] || die "no recovered directory for ${ns}/${pvc} under ${SRC}"
  printf '%s' "$d"
}

cmd_check() {
  [ -d "$SRC" ] || die "$SRC does not exist"
  local total=0
  for v in "${VOLUMES[@]}"; do
    local ns=${v%%:*} pvc=${v##*:} d sz
    d=$(src_dir_for "$ns" "$pvc")
    sz=$(du -sb "$d" | cut -f1)
    total=$((total + sz))
    printf '%-40s %8s  %s\n' "${ns}/${pvc}" "$(numfmt --to=iec "$sz")" "$(basename "$d")"
  done
  log "total $(numfmt --to=iec "$total")"

  # SQLite files are the ones that would show damage from the unclean shutdown
  if command -v sqlite3 >/dev/null; then
    while IFS= read -r db; do
      printf '%-60s %s\n' "$(basename "$db")" \
        "$(sqlite3 "file:${db}?immutable=1" 'PRAGMA integrity_check;' 2>&1 | head -1)"
    done < <(find "$SRC" -maxdepth 2 -name '*.db' -o -maxdepth 2 -name '*.sqlite' | sort)
  else
    log "sqlite3 not installed, skipping integrity checks"
  fi
}

assert_scaled_down() {
  local bad=0
  for d in monitoring:grafana monitoring:prometheus monitoring:loki \
           n8n:n8n homeassistant:homeassistant twenty:twenty; do
    local ns=${d%%:*} name=${d##*:} n
    n=$(kubectl get deploy -n "$ns" "$name" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo missing)
    [ "$n" = "0" ] || { log "deployment ${ns}/${name} has replicas=${n}, expected 0"; bad=1; }
  done
  [ "$bad" = "0" ] || die "scale everything down first (push the migration commit)"
  log "all six deployments are at 0 replicas"
}

cmd_delete() {
  assert_scaled_down
  cmd_check >/dev/null

  cat >&2 <<EOF

  About to delete these PVCs:
$(printf '      %s\n' "${VOLUMES[@]}")

  Their PVs live on pi52, which is powered off, so this data is already
  unreachable from the cluster. The copy in ${SRC} is what gets restored.

EOF
  read -r -p "  Type the word DELETE to continue: " answer
  [ "$answer" = "DELETE" ] || die "aborted"

  for v in "${VOLUMES[@]}"; do
    local ns=${v%%:*} pvc=${v##*:}
    log "deleting pvc ${ns}/${pvc}"
    # The PV is on a NotReady node, so finalizers can hang. Drop them after
    # issuing the delete rather than waiting indefinitely.
    kubectl delete pvc -n "$ns" "$pvc" --wait=false
    sleep 2
    kubectl patch pvc -n "$ns" "$pvc" -p '{"metadata":{"finalizers":null}}' 2>/dev/null || true
  done
  log "PVCs deleted. Argo CD recreates them; they stay Pending until a consumer schedules."
}

helper_manifest() {
  {
    cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${HELPER_POD}
  namespace: ${HELPER_NS}
spec:
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/hostname: ${DST_NODE}
  tolerations:
    - operator: Exists
  containers:
    - name: helper
      image: ${HELPER_IMAGE}
      command: ["sh", "-c", "sleep 7200"]
      volumeMounts:
YAML
    for v in "${VOLUMES[@]}"; do
      local ns=${v%%:*} pvc=${v##*:}
      printf '        - name: %s\n          mountPath: /data/%s/%s\n' "${ns}-${pvc}" "$ns" "$pvc"
    done
    printf '  volumes:\n'
    for v in "${VOLUMES[@]}"; do
      local ns=${v%%:*} pvc=${v##*:}
      printf '    - name: %s\n      persistentVolumeClaim:\n        claimName: %s\n' "${ns}-${pvc}" "$pvc"
    done
  }
}

cmd_restore() {
  assert_scaled_down

  # A pod can only mount PVCs from its own namespace, so one helper per namespace
  for ns in monitoring homeassistant n8n twenty; do
    local pvcs=() deadline=$((SECONDS + 300))
    for v in "${VOLUMES[@]}"; do
      [ "${v%%:*}" = "$ns" ] && pvcs+=("${v##*:}")
    done

    for pvc in "${pvcs[@]}"; do
      while ! kubectl get pvc -n "$ns" "$pvc" >/dev/null 2>&1; do
        [ $SECONDS -gt $deadline ] && die "pvc ${ns}/${pvc} was not recreated by Argo CD"
        sleep 5
      done
    done

    kubectl delete pod -n "$ns" "$HELPER_POD" --ignore-not-found --wait=true >/dev/null
    {
      cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${HELPER_POD}
  namespace: ${ns}
spec:
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/hostname: ${DST_NODE}
  tolerations:
    - operator: Exists
  containers:
    - name: helper
      image: ${HELPER_IMAGE}
      command: ["sh", "-c", "sleep 7200"]
      volumeMounts:
YAML
      for pvc in "${pvcs[@]}"; do
        printf '        - name: %s\n          mountPath: /data/%s\n' "$pvc" "$pvc"
      done
      printf '  volumes:\n'
      for pvc in "${pvcs[@]}"; do
        printf '    - name: %s\n      persistentVolumeClaim:\n        claimName: %s\n' "$pvc" "$pvc"
      done
    } | kubectl apply -f - >/dev/null

    log "waiting for helper pod in ${ns} (this provisions the volumes on ${DST_NODE})"
    local d2=$((SECONDS + 600))
    while [ "$(kubectl get pod -n "$ns" "$HELPER_POD" -o jsonpath='{.status.phase}' 2>/dev/null)" != "Running" ]; do
      [ $SECONDS -gt $d2 ] && die "helper pod in ${ns} did not start"
      sleep 5
    done

    for pvc in "${pvcs[@]}"; do
      local d
      d=$(src_dir_for "$ns" "$pvc")
      log "restoring ${ns}/${pvc} from $(basename "$d")"
      # Stream a tar in rather than kubectl cp: preserves ownership, mtimes and
      # sparse-free layout, and does not need tar present on the host side path.
      tar cf - -C "$d" . | kubectl exec -i -n "$ns" "$HELPER_POD" -- tar xf - -C "/data/${pvc}"
      local n
      n=$(kubectl exec -n "$ns" "$HELPER_POD" -- sh -c "ls -A /data/${pvc} | wc -l")
      [ "$n" -gt 0 ] || die "${ns}/${pvc} is empty after restore"
      log "${ns}/${pvc} restored (${n} top-level entries)"
    done

    kubectl delete pod -n "$ns" "$HELPER_POD" --wait=false >/dev/null
  done

  cmd_verify
  log "restore complete. Push the follow-up commit to scale everything back to 1."
}

cmd_verify() {
  kubectl get pv -o json | jq -r '.items[]
    | select(.spec.claimRef != null)
    | "\(.spec.claimRef.namespace)/\(.spec.claimRef.name) -> \(.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0] // "?") [\(.status.phase)]"' \
    | sort
}

case "${1:-}" in
  check)   cmd_check ;;
  delete)  cmd_delete ;;
  restore) cmd_restore ;;
  verify)  cmd_verify ;;
  *) die "usage: $0 {check|delete|restore|verify}" ;;
esac
