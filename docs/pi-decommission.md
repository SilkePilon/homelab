# Draining the Pis for the NVMe rebuild

Every Pi is being rebuilt: SD card out, M.2 NVMe in, fresh OS. They leave the
cluster one at a time and rejoin as blank agents, after which Longhorn replaces
`local-path` so volumes stop being node-local and nodes become hot-swappable.

Until then **server-1 is the whole cluster**. It is amd64 with an NVMe SSD and
already hosts every other stateful workload (see
[pi52-recovery.md](pi52-recovery.md) and
[monitoring-migration.md](monitoring-migration.md)).

## State before the drain

| Node | Arch | State |
|----------|-------|--------------------------------------|
| server-1 | amd64 | control plane, all bulk storage |
| pi41 | arm64 | Ready |
| pi43 | arm64 | Ready |
| pi51 | arm64 | Ready |
| pi52 | arm64 | NotReady, cordoned — SD card failed |

pi42 had already been removed from the cluster.

Only one PersistentVolume was still on a Pi: `twenty/twenty-postgres-data`
(10Gi, pi51). Everything else on the Pis was stateless and just needed
rescheduling.

## 1. Move twenty-postgres-data off pi51

`local-path` volumes are node-local. A PV provisioned on pi51 has node affinity
for pi51 and cannot follow a pod, so the data has to be copied through a
helper pod. `scripts/twenty-db-migrate.sh` does the copy; the repo carries the
scale-down and the `nodeSelector`.

Take a logical backup first — it is independent of the volume copy and is what
you fall back to if the PGDATA archive turns out to be bad:

```bash
POD=$(kubectl -n twenty get pod -l app.kubernetes.io/name=twenty-db -o jsonpath='{.items[0].metadata.name}')
kubectl -n twenty exec "$POD" -- sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dumpall -U postgres' \
  > /tmp/twenty-migration/twenty-pg_dumpall.sql
```

Then:

- **Commit A** sets `replicas: 0` on `twenty` and `twenty-db` and adds
  `nodeSelector: kubernetes.io/hostname: server-1` to `twenty-db`. Postgres
  must be stopped before PGDATA is copied, and the app must be stopped or it
  reconnects and writes.
- Run the migration:

  ```bash
  ./scripts/twenty-db-migrate.sh backup      # tar PGDATA off pi51
  ./scripts/twenty-db-migrate.sh delete-pvc  # DESTRUCTIVE: frees the pi51 data
  ./scripts/twenty-db-migrate.sh restore     # provision on server-1 and fill
  ./scripts/twenty-db-migrate.sh verify      # confirm node placement
  ```

  `local-path` uses `WaitForFirstConsumer`, so the new PV lands wherever the
  first pod that mounts it is scheduled. Pinning the restore helper to server-1
  is what places it there.
- **Commit B** sets both deployments back to `replicas: 1`.

Keep `/tmp/twenty-migration/` until Twenty has been verified healthy.

## 2. Let Argo CD survive a single-node cluster

Argo CD is installed from the upstream **HA** manifests and is not managed by
this repo. `argocd-redis-ha-server` carries a *required* pod anti-affinity on
`kubernetes.io/hostname`, so with only server-1 left two of its three replicas
are unschedulable forever and Argo CD stops reconciling.

Relax it to a preference for the duration of the rebuild:

```bash
kubectl -n argocd patch statefulset argocd-redis-ha-server --type json -p '[{
  "op": "replace",
  "path": "/spec/template/spec/affinity/podAntiAffinity",
  "value": {"preferredDuringSchedulingIgnoredDuringExecution": [{
    "weight": 100,
    "podAffinityTerm": {
      "labelSelector": {"matchLabels": {"app.kubernetes.io/name": "argocd-redis-ha"}},
      "topologyKey": "kubernetes.io/hostname"}}]}}]'
```

`argocd-redis-ha-haproxy` has the same constraint on its own replicas; it runs
a single replica here, so it needs no change.

Put the requirement back once three or more nodes are Ready again — a
`required` rule is the whole point of running the HA chart.

## 3. Drain

Nothing left on the Pis has a PVC, so a drain is enough — the pods reschedule
onto server-1 on their own and no repo change is needed for them.

```bash
for n in pi41 pi43 pi51; do
  kubectl drain "$n" --ignore-daemonsets --delete-emptydir-data --force --timeout=300s
done
```

`--force` is needed for the `node-debugger-*` pods left over from earlier
`kubectl debug node/...` sessions; they have no controller.

What moves: `argocd-*` (server, repo-server, dex, applicationset,
notifications, application-controller, redis-ha), `kube-system` traefik,
coredns, metrics-server, local-path-provisioner, csi-smb-controller,
`monitoring` kube-state-metrics and nut-exporter, `arr-stack` scraparr and
ext-to-torznab, `cloudflared` (both replicas), `twenty` redis.

All of those images are multi-arch, including the digest-pinned
`ghcr.io/silkepilon/ext-to-torznab` (an OCI index with linux/amd64).

DaemonSets — node-exporter, alloy, image-gc, csi-smb-node, svclb-traefik — are
not drained and simply disappear with the node.

Their `svclb-traefik` pods keep the Pi IPs listed as external IPs on the
`traefik` Service until the nodes go. That is cosmetic — `cloudflared` dials
`traefik.kube-system.svc.cluster.local`, not a node IP.

## 4. Remove the nodes

pi52 was already powered off, so it went immediately:

```bash
kubectl delete node pi52
```

Its `node-exporter` pod was left stuck in `Terminating` — the kubelet was gone
and could not confirm the delete — and needed
`kubectl delete pod -n monitoring <pod> --force --grace-period=0`.

The other three stay cordoned and drained until you pull their SD cards.
Deleting the node object while `k3s-agent` is still running is pointless: the
agent re-registers within seconds. Power the Pi off first, then:

```bash
kubectl delete node pi41   # and pi43, pi51, as each comes down
```

## After the rebuild

When a Pi rejoins with its NVMe, before it takes on any storage:

- Apply the kubelet thresholds from [node-storage.md](node-storage.md) — the
  disk is bigger but the defaults are still wrong.
- Install Longhorn and migrate the `local-path` PVCs to it. Once volumes are
  replicated, drop every `nodeSelector: kubernetes.io/hostname: server-1` in
  `apps/` — they exist only because `local-path` pins data to a node. They are
  in `apps/monitoring/{prometheus,grafana,loki}.yaml`,
  `apps/homeassistant/deployment.yaml`, `apps/n8n/deployment.yaml`,
  `apps/twenty/{deployment,postgres}.yaml`.
- Restore the `required` anti-affinity on `argocd-redis-ha-server`.
- `arr-stack` is gated on the `arr-downloads=true` node label rather than a
  hostname, because it needs the node that holds the 500Gi download volume.
  Move the label, not the manifests.
