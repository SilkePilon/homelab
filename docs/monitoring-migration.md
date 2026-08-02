# Moving the monitoring stack from pi52 to server-1

## Why

pi52 runs on an SD card that is saturated:

```
pi52 mmcblk0p2  io_busy%: 102.1
pi52 mmcblk0p2  write MB/s: 3.26
pi52 mmcblk0p2  read MB/s: 0.00
```

100% of the time busy while sustaining ~3 MB/s. Symptoms traced back to this:

- Grafana logged continuous `database is locked (5) (SQLITE_BUSY)` and had
  restarted 11 times.
- Pulling `docker.n8n.io/n8nio/n8n:2.33.3` (379 MB) took **32m46s**.
- Home Assistant sat in `ContainerCreating` for 45 minutes.
- An Alloy pod was stuck terminating for over 30 minutes.

Grafana, Prometheus and Loki are the heaviest writers in the cluster and all
three had their `local-path` volumes on pi52. server-1 is amd64 with a 474Gi
SSD, so they move there.

## Shape of the migration

`local-path` volumes are node-local. A PV provisioned on pi52 has node affinity
for pi52 and cannot follow a pod to another node — the data has to be copied
and the PVCs recreated.

`local-path` also uses `WaitForFirstConsumer`, so a new PVC does not bind until
a pod that mounts it is scheduled. That is what puts the new volume on
server-1: the pod carries `nodeSelector: kubernetes.io/hostname: server-1`, so
the provisioner creates the directory there.

Two commits bracket the work:

- **Commit A** adds the `nodeSelector` and sets `replicas: 0`, so the volumes
  are quiescent while being copied.
- **Commit B** sets `replicas: 1` once the data is in place on server-1.

## Runbook

Set the PV names once — they are needed throughout:

```bash
for pvc in grafana-data prometheus-data loki-data; do
  echo "$pvc $(kubectl get pvc -n monitoring $pvc -o jsonpath='{.spec.volumeName}')"
done
```

### 1. Stop the writers (commit A)

Push commit A and wait for all three Deployments to report 0 replicas:

```bash
kubectl get deploy -n monitoring grafana prometheus loki
```

Nothing should be mounting the volumes now. This alone takes the write load off
pi52's SD card, so everything else on that node speeds up.

### 2. Back up each volume off pi52

A helper pod pinned to pi52 mounts the still-bound PVCs read-only and streams a
tar to the workstation. One PVC at a time keeps the SD card from thrashing.

```bash
./scripts/monitoring-migrate.sh backup
```

Verify the archives are non-trivial before continuing:

```bash
ls -lh /tmp/monitoring-migration/
```

**Do not proceed if any archive is 0 bytes or the script reported an error.**

### 3. Delete the old PVCs

This deletes the pi52 data — the `local-path` reclaim policy is `Delete`, so
the provisioner removes the directory on the node. The backups from step 2 are
the only copy at this point.

```bash
./scripts/monitoring-migrate.sh delete-pvcs
```

Argo CD recreates the PVCs from git within a couple of minutes. They stay
`Pending` because nothing consumes them yet.

### 4. Restore onto server-1

A restore pod pinned to server-1 mounts the three new PVCs. Scheduling that pod
is what makes `local-path` provision the directories on server-1. The script
then streams each archive back in.

```bash
./scripts/monitoring-migrate.sh restore
```

Confirm the new PVs landed on the right node:

```bash
kubectl get pv -o json | jq -r '.items[]
  | select(.spec.claimRef.namespace=="monitoring")
  | "\(.spec.claimRef.name) -> \(.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0])"'
```

All three must say `server-1`.

### 5. Start the stack again (commit B)

Set `replicas: 1` on the three Deployments and push. Then:

```bash
kubectl get pods -n monitoring -o wide
```

All three should be `Running` on server-1. Check Grafana keeps its dashboards
and users, and that Prometheus still serves historical data.

## Rollback

Before step 3 the original data is untouched on pi52 — revert commit A and the
stack comes back exactly as it was.

After step 3 the archives in `/tmp/monitoring-migration/` are the only copy.
Keep them until the stack has been verified healthy on server-1.

## Afterwards

pi52 still hosts `homeassistant-config`, `n8n-data` and `twenty-server-data` on
the same SD card. That card should be replaced regardless — moving monitoring
off it relieves the symptom, not the cause.
