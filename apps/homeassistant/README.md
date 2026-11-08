# Home Assistant

Home Assistant Container (no Supervisor, so no Add-ons). `hostNetwork: true`
because discovery needs mDNS/SSDP on the LAN; `hostPort: 8123` follows from
that. Config lives on the Longhorn PVC `homeassistant-config`, not in git.

Reachable at `ha.silkepilon.dev` via Cloudflare Tunnel → Traefik → the
ClusterIP Service.

## What is in git and what is not

| Lives in git (this directory) | Lives on the PVC (UI-managed) |
| --- | --- |
| `packages/*.yaml` — Prometheus-backed homelab sensors, roll-ups, phone alerts | integrations (config entries), dashboards, UI automations |
| `arr-aliases.yaml` — hyphen-free names for the *arr apps | `configuration.yaml`, `.storage/`, Marketplace downloads |

`packages/` is turned into the `homeassistant-packages` ConfigMap and mounted
read-only at `/config/packages`; `configuration.yaml` loads it with
`homeassistant: packages: !include_dir_named packages`. The ConfigMap name
carries a content hash, so pushing a change rolls the pod and HA loads it.

`packages/homelab_cluster.yaml` is generated — edit
`generate-cluster-package.py` (node list, queries) and re-run it. It needs the
`argocd` scrape job in `apps/monitoring/prometheus.yaml` for the Argo CD
sensors.

## Dashboards

Home (the system default panel), Homelab and Library live in HA storage, but
their source is `dashboards/build.py` — edit that and re-run it rather than
editing in the UI, or the next run overwrites your UI changes. Usage is in the
script's docstring (port-forward + a long-lived token).

## Marketplace (built-in HACS)

Since 2026.x HACS is part of core as the **Marketplace** integration — do not
install the old `hacs/integration` on top of it. Installed through it:
Bubble Card, Mushroom, card-mod, mini-graph-card, auto-entities, button-card,
apexcharts-card, flex-horseshoe-card, ha-sankey-chart, clock-weather-card,
horizon-card, hourly-weather, calendar-card-pro, mini-media-player,
battery-state-card (frontend) and Bambu Lab (integration, downloaded but not yet set up — it
needs the printer's access code). Downloads land in
`/config/custom_components` and `/config/www/community` on the PVC.

## Integrations and the *arr hostname bug

Sonarr, Radarr and Lidarr go through `aiopyarr`, which mangles any hostname
containing a hyphen — `lidarr.arr-stack.svc.cluster.local` becomes the host
`lidarr.arr-` and the config flow reports `cannot_connect`. HA therefore talks
to `http://<app>.homeassistant.svc.cluster.local:<port>`, ExternalName aliases
from `arr-aliases.yaml`. Never point them at a ClusterIP again; that is what
the old Sonarr entry did. qBittorrent has no such bug and uses
`qbittorrent.arr-stack.svc.cluster.local:8080` directly. API keys come from the
`scraparr-api-keys` Secret in `arr-stack`.

## Backups

Automatic backups run daily at 04:30 to the `backup.local` agent and keep 7.
That agent writes to `/config/backups` — the same Longhorn volume — so it
protects against bad config, not against losing the volume.

## The reverse-proxy trap (NOT GitOps — read this)

Since HA **2026.x the `http:` integration is configured in storage**, not in
`configuration.yaml`. `homeassistant/components/http/config.py` migrates YAML
into `/config/.storage/http` exactly once, sets `yaml_migration_done: true`,
and from then on **ignores the YAML entirely** — an `http:` block added later
does nothing but raise a "YAML still present after migration" repair issue.

That is a nasty failure mode here, because this instance first booted with no
`http:` block at all, so the migration was marked done against an empty config
and the store never learned about the proxy.

Symptom: HA answers **every** proxied request with `400`, logging

```
A request from a reverse proxy was received from 192.168.0.233, but your
HTTP integration is not set-up for reverse proxies
```

The browser still renders the HA shell — the frontend is served by a service
worker out of cache — so the only visible failure is the websocket:

```
Firefox can't establish a connection to the server at
wss://ha.silkepilon.dev/api/websocket
Unable to connect to Home Assistant. Retrying in 29 seconds...
```

which reads like a websocket/proxy bug and is not one.

### What the store has to contain

`use_x_forwarded_for` and `trusted_proxies` are `vol.Inclusive` — set both or
neither. Because the HA pod is on `hostNetwork`, traffic from the Traefik pod
is SNATed to the IP of **whichever node Traefik is running on**, so every node
IP has to be trusted, not just the current one:

```jsonc
// /config/.storage/http  →  data.stable
"use_x_forwarded_for": true,
"trusted_proxies": [
  "127.0.0.1/32", "::1/128",
  "10.42.0.0/16",       // pod CIDR
  "192.168.0.9/32",     // hp-elitedesk-800-g6-i7
  "192.168.0.98/32",    // raspberrypi-5-16gb-1
  "192.168.0.103/32",   // raspberrypi-5-8gb-3
  "192.168.0.158/32",   // hp-elitedesk-800-g5-i7
  "192.168.0.223/32",   // hp-elitedesk-800-g6-i5
  "192.168.0.233/32",   // raspberrypi-5-8gb-1
  "192.168.0.239/32"    // raspberrypi-5-8gb-2
]
```

Add or renumber a node and this list needs the new IP, or HA starts returning
`400` whenever Traefik lands there.

The supported way to edit it is Settings → System → Network, which writes the
change to the `pending` slot and **auto-reverts after five minutes unless you
confirm it**. Editing `data.stable` in the file directly and restarting skips
the trial; the file is root-owned `0600` and must stay that way.

### Verifying

Direct-to-pod tells you nothing — it is unproxied and always worked. Test
through Traefik:

```sh
kubectl run curltest --rm -i --restart=Never --image=curlimages/curl -- sh -c "
curl -so /dev/null -w 'http %{http_code}\n' -H 'Host: ha.silkepilon.dev' \
  http://traefik.kube-system.svc.cluster.local/
curl -so /dev/null -w 'ws   %{http_code}\n' -H 'Host: ha.silkepilon.dev' \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  http://traefik.kube-system.svc.cluster.local/api/websocket"
```

Want `http 200` and `ws 101`. Both `400` means the store is wrong again.

Testing the public hostname needs `--http1.1`; over HTTP/2 curl drops the
`Connection`/`Upgrade` headers (they are illegal in h2) and Cloudflare hands
back a `400` that has nothing to do with this.

## Bluetooth

The integration talks to BlueZ over the host's **D-Bus system bus**, not to
`/dev` or to the adapter directly, so the container needs two things:

- a bind mount of `/run/dbus/system_bus_socket`, otherwise setup fails with
  `hci0 (…): DBus service not found; make sure the DBus socket is available:
  [Errno 2] No such file or directory`
- `NET_ADMIN` + `NET_RAW`, otherwise it logs
  `Missing NET_ADMIN/NET_RAW capabilities for Bluetooth management` and cannot
  reset a wedged adapter

Both live in `deployment.yaml`. The adapter itself is enumerated from
`/sys/class/bluetooth`, which the pod already sees because of `hostNetwork` —
that is why HA lists `hci0` and its MAC even while the D-Bus setup is failing.

The pod is deliberately **not pinned**, and Bluetooth adapters are per-node:
every node currently has an `hci0` (the EliteDesks an Intel radio, the Pis the
onboard one) and runs `bluetooth.service`, so the pod stays schedulable
everywhere. But HA keys its config entry on the adapter's MAC, so rescheduling
onto a different node surfaces a *new* adapter and leaves the old entry behind
as unavailable. Delete the stale entry, or pin the Deployment with a
`nodeSelector` if that churn is not acceptable.

Host-side check when it breaks:

```sh
ls /sys/class/bluetooth/         # want hci0
systemctl is-active bluetooth    # want active
ls -l /run/dbus/system_bus_socket
```
