# Longhorn node prerequisites

Applied 2026-08-12 for Longhorn v1.12, ahead of replacing `local-path` (see
[pi-decommission.md](pi-decommission.md#after-the-rebuild)). Longhorn is installed
(see [`apps/longhorn/README.md`](../apps/longhorn/README.md)) and all stateful
volumes were migrated onto it on 2026-08-12; see
[high-availability.md](high-availability.md).

None of this is GitOps. It is per-node host configuration and has to be
re-applied after a reflash.

## Which engine runs where

| Engine | Nodes | Why |
|--------|-------|-----|
| V1 | all 7 | Stable engine, iSCSI-based |
| V2 (SPDK) | the 3 amd64 nodes only | Pis cannot run it, see below |

> [!IMPORTANT]
> **The V2 data engine cannot run on the Raspberry Pis.** The Raspberry Pi OS
> kernel (`6.18.39+rpt-rpi-2712`) is built without the required support:
>
> ```
> /proc/meminfo             no HugePages lines at all
> /sys/kernel/mm/hugepages/ absent
> kernel config             CONFIG_UIO=m, CONFIG_UIO_PDRV_GENIRQ=m only
>                           (no CONFIG_HUGETLBFS / CONFIG_VFIO / CONFIG_NVME_TCP)
> modules                   vfio-pci, uio_pci_generic, nvme-tcp all absent
> ```
>
> This is not a package that can be installed — it needs a custom kernel.
> Longhorn allows a mixed cluster, so enable V2 per-node on the amd64 boxes and
> leave the Pis on V1.

## V1 requirements (all nodes)

Packages:

| Distro | Packages |
|--------|----------|
| Debian (Pis) | `open-iscsi nfs-common cryptsetup dmsetup util-linux curl gawk grep` |
| Fedora (amd64) | `iscsi-initiator-utils nfs-utils cryptsetup device-mapper util-linux curl gawk grep` |

Service and modules:

```bash
sudo systemctl enable --now iscsid
sudo modprobe iscsi_tcp dm_crypt nfs
```

Persisted in `/etc/modules-load.d/`: `iscsi_tcp.conf`, `longhorn-dm-crypt.conf`,
`longhorn-nfs.conf`.

Also required and already satisfied: mount propagation `shared`, a filesystem
supporting file extents (Pis `ext4`, amd64 `xfs`), Kubernetes >= v1.25
(running v1.35.5), and `bash curl findmnt grep awk blkid lsblk`.

## V2 / SPDK requirements (amd64 only)

```bash
sudo modprobe vfio_pci uio_pci_generic nvme-tcp ublk_drv
```

Persisted in `/etc/modules-load.d/longhorn-v2.conf`.

HugePages — 2GiB (1024 x 2MiB) per node. Allocated at runtime **and** persisted
via grubby, so no reboot was needed:

```bash
echo 1024 | sudo tee /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages
sudo grubby --update-kernel=ALL --args="hugepagesz=2M hugepages=1024"
```

The kubelet only reports hugepage capacity at startup, so `k3s`/`k3s-agent`
must be restarted before the Node object shows it:

```bash
kubectl get nodes -o custom-columns='NAME:.metadata.name,HUGEPAGES:.status.capacity.hugepages-2Mi'
# amd64 nodes must show 2Gi, Pis show <none>
```

> [!NOTE]
> That 2Gi is reserved and unavailable to normal workloads. `server-1` is the
> tightest node — it hosts every workload and went from ~7.2Gi to ~5.3Gi
> available. To reclaim it, drop the grubby args and reboot.

## Verifying

Longhorn ships its own checker. Run it from a workstation with a kubeconfig —
it needs the `longhorn-system` namespace to exist:

```bash
curl -sSfL -o longhornctl \
  https://github.com/longhorn/cli/releases/download/v1.12.0/longhornctl-linux-amd64
chmod +x longhornctl
kubectl create namespace longhorn-system
./longhornctl check preflight                 # V1
./longhornctl check preflight --enable-spdk   # V2
```

### Expected non-problems

Three findings are safe to ignore, and knowing that saves chasing them:

- **`[KernelModules] nfs is not loaded` on the Pis** — false positive. `nfs` is
  compiled into the Raspberry Pi kernel (`modinfo -n nfs` returns `(builtin)`),
  so it never appears in `lsmod`, which is what the checker greps. The same run
  reports `[NFSv4] NFS4 is supported`.
- **`[MultipathService] multipathd is not found`** — desirable. multipathd
  claiming Longhorn's devices causes volume failures; not having it is correct.
- **SPDK/HugePages errors on the Pis** — expected and unfixable, see above.

Anything else in `error:` is real. `dm_crypt is not loaded` was a genuine miss
on all seven nodes, caught only by this checker.

## After a reflash

Re-run the package install, `systemctl enable --now iscsid`, the `modprobe`
lines and the `/etc/modules-load.d/` files. On amd64 also redo the hugepages
and restart the kubelet. Then re-run `longhornctl check preflight`.
