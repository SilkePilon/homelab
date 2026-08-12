# Node storage and garbage collection

> [!NOTE]
> Volumes now live on Longhorn with 3 replicas, not `local-path` — see
> [high-availability.md](high-availability.md). Only `arr-downloads` is still
> `local-path`. The rebuild is done. The SD-card Pis are gone and four Raspberry Pi 5 boards
> on M.2 NVMe joined the cluster on 2026-08-12 — see
> [raspberry-pi-5-nodes.md](raspberry-pi-5-nodes.md). Disks are now 238Gi
> rather than 28-58Gi, so the pressure that motivated the thresholds below is
> much lower, but the kubelet defaults are still wrong.

Seven nodes as of 2026-08-12:

| Node                   | Arch  | Disk  | RAM  | Notes                           |
|------------------------|-------|-------|------|---------------------------------|
| hp-elitedesk-800-g5-i7               | amd64 | 475Gi | 16Gi | control plane, all bulk storage; plus an unused 149Gi `sda` |
| hp-elitedesk-800-g6-i5 | amd64 | 475Gi | 16Gi | i5-10500, 12 CPU                |
| hp-elitedesk-800-g6-i7 | amd64 | 475Gi | 16Gi | i7-10700, 16 CPU                |
| raspberrypi-5-16gb-1   | arm64 | 238Gi | 16Gi |                                 |
| raspberrypi-5-8gb-1    | arm64 | 238Gi | 8Gi  |                                 |
| raspberrypi-5-8gb-2    | arm64 | 238Gi | 8Gi  | PCIe Gen 2, ~half read speed    |
| raspberrypi-5-8gb-3    | arm64 | 238Gi | 8Gi  |                                 |

The three amd64 nodes each reserve 2Gi as hugepages for the Longhorn V2 data
engine, so their schedulable memory is ~2Gi lower than the table shows. See
[longhorn-prerequisites.md](longhorn-prerequisites.md).

Cleanup has three layers. Two are in this repo; the kubelet one is not.

## 1. Revision history (GitOps)

Every app's `kustomization.yaml` patches `revisionHistoryLimit: 2` onto its
Deployments and DaemonSets. The Kubernetes default of 10 left 30 empty
ReplicaSets in the cluster, and each retained revision pins its container image
on the node so kubelet will not reclaim it.

## 2. Scheduled image prune (GitOps)

`apps/node-maintenance/image-gc.yaml` runs a DaemonSet that executes
`crictl rmi --prune` on every node every 6 hours. Images referenced by an
existing container are never removed, so running workloads are unaffected.

It is a DaemonSet with a sleep loop rather than a CronJob because a CronJob
schedules a single pod and cannot fan out across nodes.

`crictl` on a k3s node is a symlink to the k3s binary and depends on the k3s
data dir, so it cannot be mounted into a container. The pod therefore runs
`nsenter -t 1 -m -u -i -n -p -- crictl ...` to use the host's own binary. That
requires `privileged: true` and `hostPID: true`.

Check it:

```bash
kubectl logs -n node-maintenance -l app=image-gc --prefix --tail=5
```

## 3. Kubelet thresholds (per node, NOT GitOps)

Kubelet only prunes images once disk usage crosses
`imageGCHighThresholdPercent`, which defaults to 85%. On a 28Gi Pi that means
roughly 24Gi consumed before anything is reclaimed. Lower it.

This is node configuration and Argo CD cannot manage it. Apply on **each** node.

On every node, edit `/etc/rancher/k3s/config.yaml` (create it if absent):

```yaml
kubelet-arg:
  - "image-gc-high-threshold=70"
  - "image-gc-low-threshold=55"
  # Reclaim exited containers sooner than the 2-hour / unlimited defaults
  - "minimum-container-ttl-duration=1m"
  - "maximum-dead-containers-per-container=1"
```

Then restart the agent:

```bash
# on the control plane (hp-elitedesk-800-g5-i7)
sudo systemctl restart k3s
# on the Pis
sudo systemctl restart k3s-agent
```

Verify the flags took effect:

```bash
kubectl get --raw "/api/v1/nodes/pi42/proxy/configz" | jq '.kubeletconfig | {imageGCHighThresholdPercent, imageGCLowThresholdPercent}'
```

`image-gc-low-threshold` must stay below `image-gc-high-threshold`, or kubelet
refuses to start.

## Volumes

Orphaned volume directories need no separate job. The `local-path` StorageClass
uses the `Delete` reclaim policy, so when a PVC is deleted the provisioner
removes `/var/lib/rancher/k3s/storage/<pv-name>` on the owning node.

To confirm nothing has been left behind after a failed delete, compare the
directories against live PVs:

```bash
kubectl get pv -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort > /tmp/live-pvs
kubectl debug node/pi52 -it --image=busybox:1.38.0 --profile=sysadmin -- \
  ls /host/var/lib/rancher/k3s/storage
```

Anything in that listing and absent from `/tmp/live-pvs` is orphaned. Delete by
hand after checking what it holds — nothing automated should remove volume
data.

## Current usage

```bash
for n in $(kubectl get nodes -o name | cut -d/ -f2); do
  kubectl get --raw "/api/v1/nodes/$n/proxy/stats/summary" \
  | jq -r --arg n "$n" '"\($n) fs=\(.node.fs.usedBytes/1073741824|floor)Gi/\(.node.fs.capacityBytes/1073741824|floor)Gi images=\(.node.runtime.imageFs.usedBytes/1073741824|floor)Gi"'
done
```
