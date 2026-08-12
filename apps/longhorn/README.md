# Longhorn

Distributed block storage. Replaces `local-path`, whose volumes are pinned to
the node that provisioned them — the reason every stateful app in `apps/` still
carries `nodeSelector: kubernetes.io/hostname: hp-elitedesk-800-g5-i7`.

Like `tailscale`, there is no Kustomize base: the chart is rendered directly by
the Argo CD Application at
[`bootstrap/argocd/applications/longhorn.yaml`](../../bootstrap/argocd/applications/longhorn.yaml),
following the [upstream Argo CD install guide](https://longhorn.io/docs/1.12.0/deploy/install/install-with-argocd/).

Node prerequisites are host configuration and are **not** managed here — see
[docs/longhorn-prerequisites.md](../../docs/longhorn-prerequisites.md).

## Sizing

Measured 2026-08-12, before installation:

| | |
|---|---|
| Actual data in all `local-path` volumes | **9.5G** |
| Sum of PVC *requests* | 569Gi (never enforced — `local-path` only makes a directory) |
| Free space across the 7 nodes | ~2.2TB |
| At 3 replicas, 9.5G costs | ~29G |

Storage is not the constraint. The constraints are:

- **1GbE on every node.** Longhorn writes synchronously to all replicas, so
  each write crosses the network twice more than a local write. Expect higher
  write latency on `prometheus-data` (constant small writes) and
  `twenty-postgres-data` than `local-path` gave.
- **No node has a dedicated disk.** Longhorn stores to `/var/lib/longhorn` on
  each root filesystem. `storageReservedPercentageForDefaultDisk: 25` and
  `storageMinimalAvailablePercentage: 15` keep it from filling the OS disk.
  `hp-elitedesk-800-g5-i7` has an **unused 149G `sda`** that would make a proper dedicated
  disk; adding it means formatting it, so it is deliberately left alone.

## Deliberate settings

| Setting | Value | Why |
|---|---|---|
| `persistence.defaultClass` | `false` | `local-path` is still the cluster default. Two defaults is undefined behaviour. Flip this **and** unset local-path's annotation together, after migrating. |
| `defaultReplicaCount` | `3` | Cheap at 9.5G. Makes nodes hot-swappable. |
| `replicaSoftAntiAffinity` | `false` | Hard anti-affinity — never two replicas of a volume on one node. 7 nodes, 3 replicas, plenty of room. |
| `reclaimPolicy` | `Delete` | Matches `local-path` and what `node-storage.md` documents. Switch to `Retain` if you would rather orphan data than lose it to an accidental PVC delete. |
| `preUpgradeChecker.jobEnabled` | `false` | It is a Job; Argo CD would re-run it on every sync. Upstream disables it for GitOps. |
| `v2DataEngine` | *unset (off)* | Prereqs exist on the 3 amd64 nodes, but the Pi kernel cannot run SPDK. Enabling it cluster-wide would break the arm64 nodes. |
| `ServerSideApply` | `true` | 23 CRDs blow past the 262144-byte `last-applied-configuration` limit of client-side apply. |

## UI

The Longhorn UI has **no authentication of its own**. It is therefore not on
the public traefik ingress; the `longhorn-frontend` Service is annotated for
Tailscale instead:

```yaml
tailscale.com/expose: "true"
tailscale.com/hostname: longhorn
```

Reachable at `longhorn.<your-tailnet>.ts.net`.

> [!WARNING]
> Anyone on your tailnet who reaches that URL has full control of cluster
> storage, including deleting volumes. Restrict it with an ACL grant if the
> tailnet ever has users other than you.

Without Tailscale, use a port-forward instead:

```bash
kubectl -n longhorn-system port-forward svc/longhorn-frontend 8080:80
```

## Verifying

```bash
kubectl -n longhorn-system get pods
kubectl get storageclass                 # longhorn present, local-path still (default)
kubectl get nodes.longhorn.io -n longhorn-system
kubectl get disks.longhorn.io -n longhorn-system 2>/dev/null || \
  kubectl -n longhorn-system get nodes.longhorn.io -o yaml | grep -A5 diskStatus
```

Every node should appear schedulable with its disk `Ready`.

## Migrating volumes (not done yet)

Volumes do **not** move on their own. `local-path` PVs have node affinity and
cannot be reassigned, so each volume has to be copied through a helper pod —
the same shape as `scripts/twenty-db-migrate.sh`.

Order matters: stop the workload, copy, repoint the PVC, restart. Do it one app
at a time and verify before moving on.

**`arr-downloads` is intentionally excluded.** It is scratch space holding
downloads until they are copied to the NAS, currently 1.5G against a 500Gi
request. Replicating throwaway data three times would be wasteful, and if it
ever filled to its request it would need 1.5TB. It stays on `local-path`.

`arr-media` (10Ti) is already on the NAS via `smb.csi.k8s.io` and is unaffected.

Once everything else is on Longhorn:

1. Set `persistence.defaultClass: true` here and remove the default annotation
   from `local-path` in the same change.
2. Drop the `nodeSelector: kubernetes.io/hostname: hp-elitedesk-800-g5-i7` lines from
   `apps/monitoring/{prometheus,grafana,loki}.yaml`,
   `apps/homeassistant/deployment.yaml`, `apps/n8n/deployment.yaml`,
   `apps/twenty/{deployment,postgres}.yaml`.
3. Restore the `required` anti-affinity on `argocd-redis-ha-server`.
