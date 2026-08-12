# Pi-hole

Network-wide DNS ad blocking. Pi-hole v6, which is a single binary (FTL)
serving both DNS and the admin UI — there is no lighttpd and no
`/etc/dnsmasq.d` any more, so `/etc/pihole` is the only volume.

The image is multi-arch, so this schedules on any of the seven nodes.

## Deploying does not change your DNS

Adding this app starts a DNS server; it does not point anything at it. The
cluster nodes keep the resolver they already had, and nothing on the LAN uses
Pi-hole until you change the router. That was the explicit intent here.

## How it is reachable

k3s ServiceLB (klipper) backs the `LoadBalancer`, which means a DaemonSet
claims host port 53 on **every** node. So all seven node IPs are working DNS
servers no matter which node the pod is on:

```
192.168.0.158  hp-elitedesk-800-g5-i7      192.168.0.98   raspberrypi-5-16gb-1
192.168.0.223  hp-elitedesk-800-g6-i5      192.168.0.233  raspberrypi-5-8gb-1
192.168.0.9    hp-elitedesk-800-g6-i7      192.168.0.239  raspberrypi-5-8gb-2
                                           192.168.0.103  raspberrypi-5-8gb-3
```

Point the UniFi gateway's DNS at one of them. Any of the three amd64 nodes is
the better pick — they hold etcd and are the least likely to be unplugged.

> [!WARNING]
> Do **not** set a public resolver (1.1.1.1, 8.8.8.8) as the router's secondary
> DNS. Clients pick between the two freely rather than treating the second as a
> fallback, so ads leak through at random and the results look like an
> intermittent Pi-hole bug. If you want a second entry, use a **second node
> IP** — same Pi-hole, no bypass.

## The node resolver trap (NOT GitOps — read this)

Claiming host port 53 has a consequence that is easy to miss and bit this
cluster immediately on first deploy.

The CNI hostPort rule klipper installs has **no destination match**:

```
-A OUTPUT -m addrtype --dst-type LOCAL -j CNI-HOSTPORT-DNAT
-A CNI-DN-...  -p udp -m udp --dport 53 -j DNAT --to-destination 10.42.0.16:53
```

So *any* packet the node itself sends to port 53 of *any* local address is
redirected into Pi-hole. On the three Fedora nodes `/etc/resolv.conf` pointed
at systemd-resolved's stub on `127.0.0.53` — a local address — so the moment
this Service existed, every node process doing DNS was routed into a Pi-hole
that could not start yet, because pulling its image needed DNS. Deadlock. The
symptom was:

```
Failed to pull image "pihole/pihole:...": ... lookup registry-1.docker.io: Try again
```

`getent hosts` still worked and hid it, because Fedora's nsswitch has
`resolve` before `dns` and nss-resolve talks to systemd-resolved over D-Bus,
never touching port 53.

The Pis were unaffected: their resolver was the router, a remote address, so
the `--dst-type LOCAL` jump never matched.

**Fix, applied to all seven nodes.** Give each node a static public resolver so
node DNS never enters the DNAT and never depends on Pi-hole:

```bash
# both OSes — persists across reboots
sudo nmcli con mod "Wired connection 1" \
  ipv4.ignore-auto-dns yes ipv4.dns "1.1.1.1 9.9.9.9"

# Fedora only — drop the 127.0.0.53 stub, it is the local address that gets hijacked
printf '[Resolve]\nDNSStubListener=no\n' | sudo tee /etc/systemd/resolved.conf.d/99-no-stub.conf
sudo systemctl restart systemd-resolved
sudo resolvectl dns eno1 1.1.1.1 9.9.9.9      # runtime, avoids bouncing the link
sudo ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
```

Verify with `getent -s dns hosts registry-1.docker.io` — force the `dns` module,
because plain `getent hosts` will lie to you.

This is host configuration; Argo CD cannot manage it, same as the kubelet
thresholds in [docs/node-storage.md](../../docs/node-storage.md). Redo it on
any node you rebuild.

It is also the right setup independent of the bug: cluster infrastructure
resolving through the ad blocker it hosts is a dependency loop. Nodes now use
1.1.1.1 directly, so Pi-hole going down cannot stop an image pull. The
trade-off is that node queries are neither filtered nor logged, which is what
you want for infrastructure.

The pod itself avoids the same loop from the other side with `dnsPolicy: None`
— see [deployment.yaml](deployment.yaml).

### Admin UI

Not on the public Cloudflare tunnel. Whoever reaches it controls what the whole
LAN resolves, so it is exposed on the tailnet like the Longhorn UI:

```yaml
tailscale.com/expose: "true"
tailscale.com/hostname: pihole
```

`http://pihole.<your-tailnet>.ts.net`. Without Tailscale:

```bash
kubectl -n pihole port-forward svc/pihole-web 8081:80
# http://localhost:8081/admin
```

## Secret

```bash
kubectl -n pihole create secret generic pihole-secret \
  --from-literal=WEBPASSWORD="$(openssl rand -base64 18)"
```

Read it back with:

```bash
kubectl -n pihole get secret pihole-secret -o jsonpath='{.data.WEBPASSWORD}' | base64 -d; echo
```

## What is in git and what is not

`FTLCONF_*` environment variables become read-only in the web UI — Pi-hole
marks anything set by env as env-managed. That is the point: those settings
live in [deployment.yaml](deployment.yaml).

Everything else — adlists, allow/deny rules, per-client groups, local DNS
records — lives in `gravity.db` on the PVC and is managed from the UI. It is on
Longhorn with 3 replicas, so it survives losing a node, but it is **not** in
git. Export a backup from *Settings → Teleporter* if it matters to you.

## Deliberate settings

| Setting | Value | Why |
|---|---|---|
| `FTLCONF_dns_listeningMode` | `all` | Required in Kubernetes. The default (`local`) only answers clients on the pod's own subnet, and forwarded queries arrive from another node's pod CIDR — they get refused silently. |
| `dnsPolicy` | `None` + public resolvers | Stops Pi-hole resolving through the router, i.e. through itself, once the router points here. Gravity updates would otherwise fail exactly when Pi-hole is unhealthy. |
| `externalTrafficPolicy` | `Cluster` | Every node IP answers. Costs per-client stats (klipper masquerades the source IP). `Local` gives real client IPs but shrinks the external IP list to the node running the pod. |
| `strategy` | `Recreate` | RWO volume, and FTL holds an exclusive lock on its SQLite databases. |
| `FTLCONF_database_maxDBdays` | `90` | Caps query-log growth against the 2Gi PVC. |
| probes | `tcpSocket` on 53 | A Pi-hole serving its admin page but no longer resolving is the failure that matters. |

## Verifying

```bash
kubectl -n pihole get pods,svc
kubectl -n pihole get svc pihole-dns -o wide      # 7 external IPs

# resolve through a node that is NOT running the pod
dig @192.168.0.158 example.com +short
# a blocked domain should come back 0.0.0.0
dig @192.168.0.158 doubleclick.net +short
```

## Failure behaviour

Node dies: the pod reschedules in ~60s (see
[docs/high-availability.md](../../docs/high-availability.md)) and Longhorn
rebuilds the replica. DNS on the *surviving* node IPs never stops, because
klipper forwards to wherever the pod is. The only outage is the reschedule
window, and only if the router points at the node that died.

If you ever want a single floating DNS VIP instead of seven node IPs, that
means MetalLB in L2 mode, which requires disabling k3s ServiceLB — and Traefik
depends on it today. Not worth it for this.
