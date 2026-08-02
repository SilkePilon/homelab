# pi52 SD card failure and recovery

2026-08-02.

## What happened

Grafana had been restarting for weeks with `database is locked (5) (SQLITE_BUSY)`.
The probe and retry tuning in `apps/monitoring/grafana.yaml` treated the symptom.
The cause was pi52's storage.

Measured from node-exporter on pi52, with all three monitoring writers already
stopped:

```
mmcblk0p2  io_busy%:   103.4
mmcblk0p2  write MB/s:   0.76
mmcblk0p2  read MB/s:    0.00
node_load1: 23.21, procs_blocked: 3, io queue depth: 11
```

100% device utilisation while moving under 1 MB/s, with nothing meaningful
running, is error recovery — not contention. Everything else on that node
followed from it:

- Pulling `docker.n8n.io/n8nio/n8n:2.33.3` (379 MB) took **32m46s**.
- Home Assistant sat in `ContainerCreating` for 45 minutes.
- An Alloy pod was stuck terminating for over 30 minutes.
- `kubectl exec` onto the node timed out; `crictl stop --timeout 90` was ignored.
- sshd accepted TCP but never completed the banner exchange.

The node still reported `Ready` throughout. Kubelet's health checks do not
notice a disk that accepts work and never finishes it.

## Powering it off

Neither SSH nor `kubectl exec` worked, so shutdown went through a pod pinned to
the node with `spec.nodeName` (bypassing the cordon) running Magic SysRq via
`nsenter` into PID 1:

```
echo 1 > /proc/sys/kernel/sysrq
echo s > /proc/sysrq-trigger   # emergency sync
echo u > /proc/sysrq-trigger   # remount all filesystems read-only
echo o > /proc/sysrq-trigger   # power off
```

`systemctl poweroff` would have hung the same way sshd did — systemd's shutdown
path needs disk reads. SysRq is handled in the kernel. It used the already
cached `busybox` image, because pulling anything on that node took ~30 minutes.

SysRq-`s` was slow but not hung; the full sequence completed and the card was
synced and remounted read-only before power was cut. That is why the databases
came back clean.

## The card was not dead

Read in a USB reader on a workstation: **49 MB in 0.64s, ~77 MB/s**. The
filesystem mounted without complaint.

Reads healthy, writes collapsed. That is worn flash — the controller serves
reads normally, but every write triggers internal garbage collection and retry.

So a plain `cp -a` recovered everything and `ddrescue` was unnecessary; imaging
is for media that throws read errors. The card was mounted `ro,norecovery` so
no journal replay and no writes ever touched it.

```bash
udisksctl mount -b /dev/mmcblk0p2 --options ro,noload
sudo cp -a /run/media/silke/writable/var/lib/rancher/k3s/storage/. ~/pi52-recovery/
sudo chown -R "$USER:$USER" ~/pi52-recovery
```

## What was recovered

7.2G total. All SQLite databases pass `PRAGMA integrity_check`.

| Volume | Size | Contents |
|---|---|---|
| `homeassistant-config` | 12M | 23,955 states, 3,253 events, automations, scenes, scripts, secrets |
| `prometheus-data` | 6.9G | 23 TSDB blocks, WAL current to 14:59 |
| `loki-data` | 202M | chunks, tsdb-shipper, wal |
| `grafana-data` | 52M | 5 dashboards, 1 user, 2 datasources |
| `n8n-data` | 3.9M | empty — 0 workflows, 0 credentials |
| `twenty-server-data` | 3.5M | workspace attachments |

Grafana 13 keeps dashboards in unified storage, not the legacy `dashboard`
table. `select count(*) from dashboard` returns 0 on a perfectly healthy
Grafana 13 database. The real count is in `resource`:

```sql
select "group", resource, count(*) from resource group by 1,2;
```

## Restoring

`scripts/restore-pi52-volumes.sh`. `local-path` uses `WaitForFirstConsumer`, so
a new PVC does not bind until a pod that mounts it is scheduled — scheduling
the helper pod on server-1 is what places the volume there.

```bash
./scripts/restore-pi52-volumes.sh check     # sizes + integrity
./scripts/restore-pi52-volumes.sh delete    # drop the PVCs pinned to pi52
./scripts/restore-pi52-volumes.sh restore   # provision on server-1 and fill
./scripts/restore-pi52-volumes.sh verify    # confirm node placement
```

All six workloads must be at 0 replicas first; the script refuses otherwise.
Deleting the PVCs needs their finalizers patched off, because the PVs reference
a node that is gone.

## Afterwards

All six workloads now carry `nodeSelector: kubernetes.io/hostname: server-1`.
Twenty's Postgres and Redis were not moved — they live on pi51 and pi42.

Keep `~/pi52-recovery` until everything has been verified healthy on server-1.

### Before pi52 rejoins

The node object still exists, cordoned and `NotReady`. With a fresh card it will
rejoin under the same name. Before uncordoning:

- Decide what belongs on Pi-class storage at all. Prometheus, Loki and Grafana
  are the heaviest writers in the cluster and should stay on server-1's SSD.
- Apply the kubelet GC thresholds in `docs/node-storage.md` as part of the
  rebuild — a smaller image cache means less write amplification on the card.
- Consider booting from USB SSD instead of SD. This card lasted roughly 50 days
  under Prometheus and Loki write load.

To retire pi52 permanently instead:

```bash
kubectl delete node pi52
```
