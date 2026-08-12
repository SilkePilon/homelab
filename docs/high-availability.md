# High availability

Set up 2026-08-12. The goal: unplug any single node and its workloads come
back somewhere else, without ever running two copies at once. Plug the node
back in and storage re-spreads onto it.

## What changed

| Layer | Before | After |
|-------|--------|-------|
| Control plane | 1 server, SQLite | 3 servers, embedded etcd |
| Storage | `local-path`, node-local | Longhorn, 3 replicas |
| Placement | every stateful app pinned to `hp-elitedesk-800-g5-i7` | free to schedule |
| Dead-node detection | ~5 min | ~60s |

## The server-1 rename

The control-plane node was called `server-1` via a `node-name` override while
its actual hostname was already `hp-elitedesk-800-g5-i7`. The override was
removed on 2026-08-12 so it matches the two G6 minis.

A k3s node cannot be renamed in place — it registers as a new node object — so
this was staged:

1. `evictionRequested: true` on its Longhorn node, moving all 14 replicas off
   while every volume still had two healthy copies elsewhere. Zero volumes went
   degraded.
2. Drain, `k3s-uninstall.sh`, `kubectl delete node server-1` (which removes the
   etcd member).
3. Reinstall as a server with no `node-name`, rejoining under its hostname.
4. Recreate the `arr-downloads` PVC — its `local-path` PV had hard node
   affinity to `server-1` and became unschedulable.
5. Re-apply the `arr-downloads=true` node label, which arr-stack schedules on.

> [!WARNING]
> Between steps 2 and 3 etcd runs on two members, so quorum is 2 of 2 and
> losing either one breaks the cluster. Keep that window short and do not touch
> the other servers during it. Take a snapshot first:
> `sudo k3s etcd-snapshot save --name pre-rename`.

Historical docs (`pi52-recovery.md`, `monitoring-migration.md`,
`pi-decommission.md`) still say `server-1`. That is deliberate — they describe
what happened when the node had that name.

## Control plane

`hp-elitedesk-800-g5-i7` ran a single k3s server on SQLite, so losing it meant losing the API
server — nothing could reschedule anywhere and no amount of storage
replication would have helped.

It was migrated in place to embedded etcd with `cluster-init: true` (k3s
migrates the SQLite datastore on restart), and both EliteDesk minis were
rebuilt from agents into servers:

```
hp-elitedesk-800-g5-i7                 control-plane,etcd
hp-elitedesk-800-g6-i5   control-plane,etcd
hp-elitedesk-800-g6-i7   control-plane,etcd
raspberrypi-5-*          agents
```

Three etcd members tolerate losing one. **Losing two breaks quorum** and the
cluster goes read-only, so never take two of the amd64 boxes down at once.

A pre-migration backup of the SQLite datastore is on `hp-elitedesk-800-g5-i7` at
`/root/k3s-backups/`.

> [!NOTE]
> The Pis are deliberately agents. etcd is write-latency sensitive and wants
> consistent disks; keeping quorum on the three amd64 NVMe boxes is the right
> trade. Four agents is plenty of capacity.

`tls-san` lists all three server IPs, so a kubeconfig can point at any of them.
The local kubeconfig still points only at `192.168.0.158` — if `hp-elitedesk-800-g5-i7` is
down, switch the `server:` field to `192.168.0.223` or `192.168.0.9`, or use
the Tailscale API server proxy, which is not tied to one node.

## Failover timing

Default Kubernetes takes about five minutes to evict pods from a dead node.
That is now ~60s, tuned as a chain that has to be consistent end to end:

| Setting | Value | Where |
|---|---|---|
| `node-status-update-frequency` | `5s` | kubelet, **every** node |
| `node-monitor-period` | `5s` | kube-controller-manager |
| `node-monitor-grace-period` | `20s` | kube-controller-manager |
| `default-not-ready-toleration-seconds` | `40` | kube-apiserver |
| `default-unreachable-toleration-seconds` | `40` | kube-apiserver |

Node is marked `NotReady` after ~20s (4 missed kubelet updates), pods are
evicted ~40s after that. Verify the tolerations actually reached the pods:

```bash
kubectl -n n8n get pod -l app.kubernetes.io/name=n8n \
  -o jsonpath='{.items[0].spec.tolerations}' | jq
# expect tolerationSeconds: 40, not 300
```

Going much below this risks a brief network blip or a busy node looking dead,
and for an RWO volume a false failover is a real restart.

## Storage behaviour on node loss

Three Longhorn settings do the actual work. Without the first one the rest is
useless:

- **`node-down-pod-deletion-policy: delete-both-statefulset-and-deployment-pod`**
  — a pod on an unplugged node sits in `Terminating` forever, because the
  kubelet is gone and cannot confirm the delete. Its RWO volume stays attached,
  so the replacement pod can never start. Longhorn force-deletes those pods so
  the volume detaches.
- **`replica-auto-balance: best-effort`** — when a node returns, replicas
  spread back onto it instead of staying bunched on the survivors.
- **`default-data-locality: best-effort`** — keeps one replica on whichever
  node runs the pod, so reads are local and only writes cross the network.

Plus `replica-soft-anti-affinity: false`, so no two replicas of a volume ever
share a node.

## Only one copy at a time

Every stateful app is `replicas: 1` with `strategy: Recreate`. Recreate
terminates the old pod before starting the new one, which is required for a
ReadWriteOnce volume and is what stops two copies running simultaneously.
Do not switch these to `RollingUpdate` — the new pod would block forever
waiting for a volume the old pod still holds.

## Architecture constraints

Two workloads cannot run on the arm64 Pis and pin on
`kubernetes.io/arch: amd64`. These are architecture constraints, **not** host
pins — each can still use any of the three amd64 nodes, so they stay HA:

- **`signal-cli`** — its distribution bundles `libsignal-client` as an
  x86_64-only native library. On arm64 the JVM aborts with
  `no signal_jni in java.library.path`.
- **`flaresolverr`** — no arm64 image published.

`nousresearch/hermes-agent` publishes both architectures, so hermes itself runs
anywhere.

When adding an app, check the image is multi-arch before assuming it can
schedule on a Pi:

```bash
docker manifest inspect <image> | jq -r '.manifests[].platform.architecture'
```

## What is not covered

- **Two amd64 nodes down at once** breaks etcd quorum. Single-node loss only.
- **`arr-downloads`** is still `local-path` on `hp-elitedesk-800-g5-i7` and does not follow a
  failover. It is scratch space until downloads reach the NAS; replicating it
  three times would be wasteful. If `hp-elitedesk-800-g5-i7` is down, the arr apps start but
  cannot stage downloads.
- **Pods do not move back** when a node returns — only Longhorn replicas
  re-spread. A returned node picks up work at the next restart. A descheduler
  would change that, at the cost of deliberately restarting healthy pods.
- **1GbE everywhere.** Every NIC in the cluster is 1000Mb/s, including the Pis
  where it is built in and not upgradable. The switch is not the limit. With 3
  replicas each write crosses the network twice more than a local write, which
  is why `data-locality: best-effort` matters for `prometheus-data`.

## Testing it

Simulate a node failure without touching hardware:

```bash
# on a Pi agent
sudo systemctl stop k3s-agent

# watch from the workstation
kubectl get nodes -w                 # NotReady in ~20s
kubectl get pods -A -o wide -w       # rescheduled ~40s later
```

Then `sudo systemctl start k3s-agent` and confirm Longhorn rebuilds replicas
onto it:

```bash
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns='NAME:.metadata.name,ROBUST:.status.robustness'
# all should return to healthy
```

Test on a Pi first, not on one of the three etcd members.
