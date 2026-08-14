# Raspberry Pi 5 cluster nodes

Four Raspberry Pi 5 boards rebuilt onto M.2 NVMe and joined to the k3s cluster
as agents on 2026-08-12, replacing the old SD-card Pis described in
[pi-decommission.md](pi-decommission.md).

All node-level configuration here is applied **per node and is not GitOps**.
Argo CD cannot manage `config.txt`, the kernel cmdline, or the bootloader
EEPROM. Re-apply it by hand after any reflash — see
[After a reflash](#after-a-reflash).

## Inventory

| Hostname               | IP            | MAC               | RAM  | Board rev        | PCIe   |
|------------------------|---------------|-------------------|------|------------------|--------|
| `raspberrypi-5-16gb-1` | 192.168.0.98  | 2c:cf:67:d8:27:3e | 16GB | Rev 1.1 (rev 30) | Gen 2  |
| `raspberrypi-5-8gb-1`  | 192.168.0.233 | 2c:cf:67:9b:3f:ce | 8GB  | Rev 1.0 (rev 21) | Gen 2  |
| `raspberrypi-5-8gb-2`  | 192.168.0.239 | 2c:cf:67:7d:d2:c1 | 8GB  | Rev 1.0 (rev 21) | Gen 2  |
| `raspberrypi-5-8gb-3`  | 192.168.0.103 | 2c:cf:67:66:d6:50 | 8GB  | Rev 1.0 (rev 21) | Gen 3  |

`.103` is still Gen 3 only because it was unreachable when the fleet moved to
Gen 2 — see [PCIe generation](#pcie-generation). Set it to Gen 2 on recovery.

`.98` is a 16GB board, not 8GB, hence the different name. It is also a newer
BCM2712 stepping (`rev 30`) than the other three (`rev 21`).

All four run Raspberry Pi OS Lite (Debian 13 trixie), kernel
`6.18.39+rpt-rpi-2712`, k3s `v1.35.5+k3s1`, arch `arm64`.

### Disks

Every node boots from a 256GB **Samsung PM991a** (DRAM-less OEM drive) on an
M.2 HAT. These are used drives pulled from earlier service, so wear and power
cycles are non-zero from day one.

| Node                   | Serial         | Wear | Power-on hrs | Unsafe shutdowns | Media errors |
|------------------------|----------------|------|--------------|------------------|--------------|
| `raspberrypi-5-16gb-1` | S660NE3R966645 | 4%   | 1230         | 46               | 0            |
| `raspberrypi-5-8gb-1`  | S660NE3R972688 | 8%   | 3205         | 75               | 0            |
| `raspberrypi-5-8gb-2`  | S660NE3R972622 | 11%  | 3274         | 56               | 0            |
| `raspberrypi-5-8gb-3`  | S660NE2R898741 | 6%   | 2034         | 54               | 0            |

The high `unsafe_shutdowns` counts predate this rebuild and come from the
phantom power-on problem below. Watch whether they keep climbing — if they do,
something is still hard-cutting power.

Measured sequential read (`dd bs=1M iflag=direct`): **~845 MB/s at Gen 3**,
**~443 MB/s at Gen 2**. Gen 3 x1 line rate is the ceiling; the drives are not
the bottleneck.

## Power

### The 40A supply does not negotiate USB-PD

Three of the Pis run from a custom 5V 40A bench supply; it presents a bare 5V
rail with no USB-PD. The Pi 5 firmware therefore finds no power data objects
and falls back to the USB default of **900 mA (4.5W)**, then caps the USB ports
to 600 mA — even though 40A is available. A Pi 5 with NVMe idles near 4W and
peaks at 10-12W.

Fix, applied to all four via EEPROM:

```
PSU_MAX_CURRENT=5000
```

This tells the firmware to skip PD negotiation and assume a 5A supply. Verify:

```bash
# expect 5000, not 900
python3 -c "print(int.from_bytes(open('/proc/device-tree/chosen/power/max_current','rb').read(),'big'),'mA')"
```

> [!WARNING]
> Only safe because the supply genuinely delivers. Do **not** set this on a Pi
> fed from a laptop USB-A port or a small phone charger — it removes the
> firmware's protective cap on a supply that cannot back it.

### Never powering on by themselves

Symptom: press the power button to shut down, and the board turns itself back
on. Stock Pi 5 EEPROM defaults are `POWER_OFF_ON_HALT=0` and `WAKE_ON_GPIO=1`,
which leave the PMIC in a shallow `HALT` state with rails partly live. Per the
Pi docs, *"Resetting the Power Management Integrated Circuit (PMIC) can also
restart the board."* The upstream root-cause thread measured the 5V rail
overshooting to 5.8V during the shutdown transient, tripping PMIC over-voltage
protection.

Applied to all four:

```
POWER_OFF_ON_HALT=1     # PMIC to STANDBY, all outputs off
```

A deliberate `poweroff` now parks the PMIC in `STANDBY` with every output off,
which removes the live-rail path that let a PMIC glitch restart the board. The
power button wakes it from there.

> [!NOTE]
> `WAIT_FOR_POWER_BUTTON=1` was tried and **deliberately reverted**. It
> guarantees a board never powers on by itself, but it also blocks the
> automatic boot after mains power returns — a blackout would leave all four
> agents dark until someone pressed each button. Auto-recovery was judged worth
> more. Add the line back only if you want them to stay off through a power
> cut too.

If a board still wakes on its own after this, the remaining cause is hardware:
the confirmed upstream fix is a 100µF capacitor across the PSU output. You
cannot disable PMIC over-voltage protection in software.

## PCIe generation

**Gen 3 is not a certified Pi 5 configuration** — it works on most boards and
fails on some. The fleet is standardised on `dtparam=pciex1_gen=2` for that
reason: stability over the roughly 2x sequential read Gen 3 buys.

`.98` and `.233` were moved from Gen 3 to Gen 2 on 2026-08-14. The previous
value is kept alongside as `config.txt.bak-gen3` on each node. `.103` was
read-only and unreachable at the time and is still Gen 3; set it to Gen 2 when
it is recovered.

`raspberrypi-5-8gb-2` (.239) was the first node pinned to Gen 2, and is why.
At Gen 3, on the good supply, it dropped its NVMe controller roughly every 34
seconds under load:

```
nvme nvme0: controller is down; will reset: CSTS=0x3, PCI_STATUS=0x10
nvme0n1: I/O Cmd(0x2) @ LBA 4953008, I/O Error (sct 0x3 / sc 0x71)
```

`sct 0x3 / sc 0x71` is a transport error. The evidence matrix that isolated it:

|              | Gen 3                       | Gen 2                     |
|--------------|-----------------------------|---------------------------|
| Weak supply  | dead within minutes         | passed, 438 MB/s          |
| 40A supply   | failed, 8 resets in 4 min   | passed, 443 MB/s, 0 errors |

PCIe generation is the deciding variable, not the supply. Its drive was fine
(`media_errors: 0`, and the same model ran Gen 3 in the sibling boards), so the
suspect is the M.2 HAT, the ribbon cable, or that board's PCIe routing. To chase
it: swap the ribbon with a known-good Pi. If the fault follows the cable it is
cheap; if it stays with the board it is the HAT or the Pi.

Cost of the fleet-wide move: every node now runs at roughly half the sequential
disk bandwidth Gen 3 gave it (~443 vs ~845 MB/s). Accepted deliberately — none
of these nodes is disk-bandwidth bound, and a node that drops its rootfs costs
far more than the throughput does.

`.103` failing on 2026-08-14 is a second data point for the same suspect list.
Unlike `.239`, it ran stable for roughly 42 hours before its rootfs went
read-only, rather than dying within minutes — a timing signature that fits an
intermittent physical connection (ribbon seating, thermal cycling) better than
either a pure Gen 3 signal-margin fault or the APST bug below.

## NVMe APST

Independently of PCIe generation, the Pi 5 plus these DRAM-less drives hit the
well-documented `controller is down` crash when Autonomous Power State
Transition is active. Every node carries this on the kernel cmdline:

```
nvme_core.default_ps_max_latency_us=0
```

This fixed `.103`, which before it booted, ran about five minutes, then lost the
controller — leaving the kernel pingable with port 22 accepting connections but
`sshd` unable to spawn, so SSH closed with
`kex_exchange_identification: Connection closed by remote host`. **That
signature means a dead rootfs, not an auth problem.**

Verify:

```bash
sudo nvme get-feature /dev/nvme0 -f 0x0c -H | grep APSTE   # expect Disabled
```

## Kernel cmdline

`/boot/firmware/cmdline.txt` on every node, appended to the stock line:

```
cgroup_memory=1 cgroup_enable=memory nvme_core.default_ps_max_latency_us=0
```

`cgroup_enable=memory` is **mandatory for k3s**. The Pi firmware injects
`cgroup_disable=memory` into `/proc/cmdline`; the later value wins, so this
override is what actually enables the memory controller. Check with:

```bash
cat /sys/fs/cgroup/cgroup.controllers   # must include "memory"
```

> [!CAUTION]
> `cmdline.txt` must stay a **single line**. A corrupt or truncated line means
> the board will not boot and needs a monitor and keyboard to recover. A backup
> is kept at `cmdline.txt.bak` on each node.

## Fan curve

All four have the Pi 5 Active Cooler and sit stacked with little air gap, so
the fan is configured to never idle off and to reach full speed well before the
SoC throttles (~80°C).

In `/boot/firmware/config.txt`:

```
# stacked-cluster fan curve
dtparam=fan_temp0=0
dtparam=fan_temp0_hyst=0
dtparam=fan_temp0_speed=125
dtparam=fan_temp1=45000
dtparam=fan_temp1_hyst=5000
dtparam=fan_temp1_speed=175
dtparam=fan_temp2=55000
dtparam=fan_temp2_hyst=5000
dtparam=fan_temp2_speed=215
dtparam=fan_temp3=62000
dtparam=fan_temp3_hyst=5000
dtparam=fan_temp3_speed=255
```

| Level | Trip  | PWM (duty) | Stock trip | Stock PWM |
|-------|-------|------------|------------|-----------|
| 1     | 0°C   | 125 (49%)  | 50°C       | 75        |
| 2     | 45°C  | 175 (69%)  | 60°C       | 125       |
| 3     | 55°C  | 215 (84%)  | 67.5°C     | 175       |
| 4     | 62°C  | 255 (100%) | 75°C       | 250       |

Threshold 0 with hysteresis 0 is what keeps the fan permanently on — the fan
can never fall back to the off state. Idle is ~3900-4700 rpm at 30-35°C.

Too loud? Lower `fan_temp0_speed` toward 75. Nothing else needs changing.
Backup at `config.txt.bak`.

```bash
# live state
cat /sys/class/hwmon/hwmon*/fan1_input   # rpm
cat /sys/class/hwmon/hwmon*/pwm1         # 0-255
cat /sys/class/thermal/cooling_device0/cur_state   # 0-4
```

## Other node settings

- **Timezone** `Europe/Amsterdam` on all four. The stock image ships
  `Europe/London`, which made cross-node log correlation off by an hour.
- **Bootloader firmware** updated to 2026-05-26 on all four.
- **`NET_INSTALL_AT_POWER_ON`** removed from the EEPROM — it displays the
  network-install UI on every cold boot.
- **Kubelet image-GC thresholds** in `/etc/rancher/k3s/config.yaml`, per
  [node-storage.md](node-storage.md). These nodes have 238GB rather than the
  28-58GB of the SD-card era, but the defaults are still wrong:

  ```yaml
  kubelet-arg:
    - "image-gc-high-threshold=70"
    - "image-gc-low-threshold=55"
    - "minimum-container-ttl-duration=1m"
    - "maximum-dead-containers-per-container=1"
  ```

  Verify from the control plane:

  ```bash
  kubectl get --raw "/api/v1/nodes/<node>/proxy/configz" \
    | jq '.kubeletconfig | {imageGCHighThresholdPercent, imageGCLowThresholdPercent}'
  ```

- **Persistent journal** was attempted on all nodes (`Storage=persistent` plus
  `/var/log/journal`) but journald keeps falling back to the runtime journal —
  it accepts the flush request and never creates the store. Unresolved. It
  matters less than it looks: a hard power reset leaves nothing in the journal
  either way.

## Joining a node to the cluster

```bash
TOKEN=$(ssh silke@192.168.0.158 'sudo cat /var/lib/rancher/k3s/server/node-token')

curl -sfL https://get.k3s.io -o /tmp/k3s-install.sh && chmod +x /tmp/k3s-install.sh
sudo env \
  INSTALL_K3S_VERSION='v1.35.5+k3s1' \
  K3S_URL='https://192.168.0.158:6443' \
  K3S_TOKEN="$TOKEN" \
  /tmp/k3s-install.sh agent
```

The node name comes from the hostname, so set the hostname **before** joining.
Set `/etc/hosts` too, or `sudo` prints `unable to resolve host` on every call:

```bash
sudo hostnamectl set-hostname raspberrypi-5-8gb-1
sudo sed -i 's/^\(127\.0\.1\.1[[:space:]]\+\).*/\1raspberrypi-5-8gb-1/' /etc/hosts
```

## After a reflash

Reflashing the NVMe wipes everything except the EEPROM, which lives in the
Pi's own SPI flash. After writing a fresh image, re-apply:

1. Hostname and `/etc/hosts`
2. `cmdline.txt`: `cgroup_memory=1 cgroup_enable=memory nvme_core.default_ps_max_latency_us=0`
3. `config.txt`: fan curve, and `dtparam=pciex1_gen=` for that board
4. Timezone
5. `/etc/rancher/k3s/config.yaml` kubelet thresholds
6. Reboot, confirm `cgroup.controllers` lists `memory`, then join

EEPROM settings (`PSU_MAX_CURRENT`, `POWER_OFF_ON_HALT`) survive a reflash and
do not need reapplying.

## Diagnostics

```bash
# power budget the firmware negotiated (900 = PD failed, 5000 = configured)
python3 -c "print(int.from_bytes(open('/proc/device-tree/chosen/power/max_current','rb').read(),'big'),'mA')"

# undervoltage / throttling; 0x0 is clean
vcgencmd get_throttled
vcgencmd measure_temp
vcgencmd pmic_read_adc | grep -E 'EXT5V_V|VDD_CORE_A'

# PCIe link actually negotiated (8GT/s = Gen 3, 5GT/s = Gen 2)
sudo lspci -vv -s 0001:01:00.0 | grep LnkSta:

# drive health
sudo nvme smart-log /dev/nvme0 | grep -E 'critical_warning|media_errors|num_err_log|percentage_used'

# NVMe transport failures
sudo dmesg -T | grep -iE 'controller is down|I/O error'

# sequential read
sudo dd if=/dev/nvme0n1 of=/dev/null bs=1M count=2048 iflag=direct

# EEPROM
sudo rpi-eeprom-config
sudo rpi-eeprom-update
```

> [!TIP]
> When scripting over SSH with `sudo -S`, the password on stdin **clobbers any
> pipe into the sudo'd command**. `printf '...' | sudo -S tee file` writes the
> password into the file. Stage content in a temp file and `sudo cp` it
> instead. Also send sudo's stderr to `/dev/null` — its prompt has no trailing
> newline and will prefix the next line of real output.

## Open items

- `raspberrypi-5-8gb-2` is stuck at PCIe Gen 2. Swap the M.2 ribbon cable to
  find out whether it is the cable, the HAT, or the board.
- Persistent journal does not work on these nodes.
- Longhorn is still not installed. Until it is, `local-path` pins volumes to
  whichever node provisions them, and the
  `nodeSelector: kubernetes.io/hostname: server-1` entries throughout `apps/`
  have to stay. See [pi-decommission.md](pi-decommission.md#after-the-rebuild).
- The `required` anti-affinity on `argocd-redis-ha-server` is still relaxed for
  single-node operation and can be restored now that there are four agents.

## References

- [Raspberry Pi bootloader EEPROM configuration](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#raspberry-pi-bootloader-configuration)
- [Power button behaviour](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#power-button)
- [Pi 5 turns itself back on after clean shutdown](https://forums.raspberrypi.com/viewtopic.php?t=388108)
- [nvme0: controller is down (raspberrypi/linux#6661)](https://github.com/raspberrypi/linux/issues/6661)
- [Raspberry Pi 5 losing PCIe link to NVMe SSD](https://blog.michael.kuron-germany.de/2026/03/raspberry-pi-5-losing-pcie-link-to-nvme-ssd/)
- `fan_temp*` parameters: `/boot/firmware/overlays/README` on any Pi
