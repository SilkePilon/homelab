# Pelican game panel on the EliteDesks — design

[Pelican](https://pelican.dev) to host Minecraft servers, running inside the
k3s cluster on the three HP EliteDesk nodes only. Reachable on the LAN, with a
router port-forward to follow later. No Cloudflare tunnel, no Tailscale, no
NetBird in this change.

Deployed like every other app here: a Kustomize base at `apps/pelican/`, an
Argo CD Application at `bootstrap/argocd/applications/pelican.yaml`, and one
Secret created by hand.

## Pelican in two parts, and why Wings is the hard part

Pelican is two programs:

- **Panel** — the PHP web UI and API. An ordinary stateful web app; SQLite by
  default. Trivial to run as a pod.
- **Wings** — the daemon that actually runs game servers. It runs each server
  as a **Docker container** through the Docker Engine API. It does not speak
  containerd or CRI.

Every node here runs k3s on containerd. There is no Docker daemon, and the
Kubernetes backend for Wings is an open upstream PR (`pelican/wings#193`), not
in any release (latest is `v1.0.0-beta29`).

Three ways to bridge this were considered:

| Option | Fit | Cost |
| --- | --- | --- |
| **A. Docker-in-Docker sidecar** (chosen) | Fully GitOps. Nothing installed on hosts. Game servers live inside the pod's cgroup, so kubelet accounting and the node's schedulable memory stay honest. | Pod is `privileged`. |
| B. Docker CE on the hosts, Wings mounts `/var/run/docker.sock` | Closest to upstream docs. | Per-node manual install on Fedora, outside GitOps. Game containers run outside kubelet's cgroups, invisible to the scheduler. Two runtimes sharing iptables. Nodes are not even reachable over SSH from the workstation. |
| C. Community `pelican-k8s` (Kubernetes-native backend) | Cleanest long-term fit: game servers become Pods. | Alpha, `v1alpha1`, forked Wings. |

Option A. Wings honours `DOCKER_HOST` (its client is built with
`client.FromEnv`), so a `docker:dind` sidecar listening on
`tcp://127.0.0.1:2375` needs no patches.

## Why nothing is on the tunnel or the tailnet

Minecraft is raw TCP. A Cloudflare tunnel hostname carries HTTP only, the same
wall the MySQL design hit. Tailscale and NetBird both need either a client on
every player's machine or a public relay in front. The decision for now is a
plain router port-forward to a node IP, so everything is exposed on the LAN
through k3s ServiceLB `LoadBalancer` Services, exactly like `pihole-dns`.

**Consequence, accepted:** the Panel URL is pinned to one node IP
(`192.168.0.158`). The pod survives that node dying — Longhorn volumes, amd64
nodeSelector — but the URL does not, until it is pointed at another node IP.
A Pi-hole local DNS name is the obvious later fix and is out of scope here.

## Network layout

Two `LoadBalancer` Services. ServiceLB claims each port on **every** node, so
any node IP answers, and the router can forward to any HP node.

| Service | Port | Backend | Who talks to it |
| --- | --- | --- | --- |
| `pelican-panel` | 8088 | panel :80 | browser |
| `pelican-wings` | 8080 | wings :8080 | Panel **and the browser directly** — console websocket and file uploads go browser → Wings, not through the Panel |
| `pelican-wings` | 2022 | wings :2022 | SFTP clients |
| `pelican-wings` | 25565–25569 | game containers | Minecraft clients |

Five game ports. Adding more is a Service edit plus a Panel allocation.

Panel node definition (done in the Panel UI, not in git): FQDN
`192.168.0.158`, daemon port 8080, SFTP port 2022, **no SSL**, allocations
`0.0.0.0` ports 25565–25569. The Panel is plain HTTP so the browser never
mixes an HTTPS page with an HTTP websocket.

## Manifests

`apps/pelican/`, from `apps/_template/` with `ingress.yaml` dropped.

### `namespace.yaml`

Namespace `pelican`, labelled `app.kubernetes.io/part-of: homelab`.

### `pvc.yaml`

All `storageClassName: longhorn`, `ReadWriteOnce`. Longhorn so the pods can
move between the three EliteDesks; ReadWriteOnce is fine because every volume
is mounted by exactly one pod.

| PVC | Size | Mounted by | Holds |
| --- | --- | --- | --- |
| `pelican-panel-data` | 2Gi | panel | SQLite DB, `.env`, `APP_KEY`, plugins |
| `pelican-wings-data` | 50Gi | wings **and** dind | server volumes, backups, archives |
| `pelican-docker` | 20Gi | dind | Docker image and container layers |

### `panel.yaml`

- Image `ghcr.io/pelican/panel:v1.0.0-beta38`. Ships Caddy on :80, so no
  extra web server.
- `replicas: 1`, `strategy: Recreate`, nodeSelector `kubernetes.io/arch: amd64`.
- Env: `APP_URL=http://192.168.0.158:8088`, `APP_ENV=production`,
  `APP_DEBUG=false`, `BEHIND_PROXY=true`, `XDG_DATA_HOME=/pelican-data`,
  `MAIL_DRIVER=log`, `TZ=Europe/Amsterdam`. `LE_EMAIL` is not set — no Let's
  Encrypt without a public hostname.
- `BEHIND_PROXY=true` matters. The image's Caddyfile uses `APP_URL` as its
  site address, so without it Caddy would listen on `:8088` and answer only
  `Host: 192.168.0.158`, and a probe against the pod IP would get an empty
  reply. With it Caddy listens on `:80` for any Host, auto-HTTPS is off and
  `ASSET_URL` is set from `APP_URL`. ServiceLB is, in effect, the proxy.
- Volume `pelican-panel-data` at `/pelican-data`. The compose file also mounts
  a `plugins` subpath at `/var/www/html/plugins`; done here with `subPath`.
- No SQLite/cache/session/queue env: the defaults (SQLite, filesystem,
  filesystem, database) are what the installer writes, and they suit one user.
- Resources: requests 100m / 256Mi, limits 1 CPU / 1Gi.
- Probes: `httpGet` on `/up` port 80, Laravel's health route and what the
  image's own `HEALTHCHECK` uses.
- First boot runs migrations, which is slow on a fresh Longhorn volume:
  startup probe with a generous failure threshold.

### `wings.yaml`

Deployment, `replicas: 1`, `strategy: Recreate`, nodeSelector `amd64`.

**Init container** `config`: copies `/secret/config.yml` to
`/etc/pelican/config.yml` in an emptyDir. Wings can rewrite its config when
the Panel pushes an update, which fails on a read-only Secret mount. The
Secret is the source of truth; the emptyDir is a working copy.

**Container `wings`** — `ghcr.io/pelican/wings:v1.0.0-beta29`.

- Env `DOCKER_HOST=tcp://127.0.0.1:2375`, `TZ`.
- Mounts: emptyDir `/etc/pelican`, PVC `pelican-wings-data` at
  `/var/lib/pelican`, emptyDir `/tmp/pelican`, emptyDir `/var/log/pelican`.
- Ports 8080 (api) and 2022 (sftp). Game ports are published by dind into the
  pod network namespace and need no `containerPort` entry.
- Readiness/liveness: `tcpSocket` 8080. `/api/system` needs a bearer token,
  so an unauthenticated HTTP probe would read 401 as failure.

**Container `dind`** — `docker:28.5.2-dind`, `privileged: true`, run as a
native sidecar (`initContainers` entry with `restartPolicy: Always`) so it is
up and past its startup probe before Wings starts.

- Args `dockerd --host=unix:///var/run/docker.sock --host=tcp://127.0.0.1:2375`.
  Bound to loopback on purpose: the image entrypoint's default listener is
  `0.0.0.0:2375`, which would offer an unauthenticated, root-equivalent Docker
  API to every pod in the cluster on the pod IP. The entrypoint only injects
  that default when the first argument is not `dockerd`.
- Env `DOCKER_TLS_CERTDIR=""` so dind does not insist on TLS.
- Mounts: PVC `pelican-docker` at `/var/lib/docker`, PVC `pelican-wings-data`
  at `/var/lib/pelican`, emptyDir `/tmp/pelican`.

**Why two of the mounts appear in both containers at the same path.** Wings
does not copy files into containers; it asks dockerd to bind-mount
`/var/lib/pelican/volumes/<uuid>` and `/tmp/pelican/<uuid>/install.sh`.
dockerd resolves those paths in *its own* mount namespace. If the paths differ
between the two containers, or exist in only one, every server install fails
with an empty or missing directory.

**Resources.** Limits are per container. Game containers run inside the
`dind` container's cgroup, so that is where the cap goes: `dind` requests
500m / 2Gi, limits 6 CPU / 8Gi. `wings` itself is small: requests 50m / 128Mi,
limits 500m / 512Mi. Per-server memory is set in the Panel; the sum should
stay under the dind limit. The EliteDesks have ~14Gi allocatable and sit at
4–6Gi used.

### `service.yaml`

The two `LoadBalancer` Services from the table above, `externalTrafficPolicy:
Cluster` for the same reason as `pihole-dns`: every node IP answers, at the
cost of source-IP masquerading. Minecraft's own logs will show the node's IP
rather than the player's. Acceptable.

### `kustomization.yaml`

`namespace: pelican`, the resources, the `app.kubernetes.io/part-of: homelab`
label with `includeSelectors: false`, and the `revisionHistoryLimit: 2`
patches every base carries.

### `bootstrap/argocd/applications/pelican.yaml`

From `_template.yaml.tpl` with `<APP_NAME>` replaced. `prune`, `selfHeal`,
`CreateNamespace=true`, `ServerSideApply=true` — unchanged.

### `README.md`

Bootstrap sequence below, the two-container explanation, and the port table.

## Secret and bootstrap order

There is a chicken-and-egg: the Wings config, including its auth token, is
generated by the Panel when a Node is created in the UI. So:

1. Push. Argo CD creates the namespace and syncs. Panel comes up; the Wings
   pod sits in `Init:0/2 (FailedMount: the Secret is missing)` because the Secret is
   missing. That is expected.
2. Open `http://192.168.0.158:8088/installer`. SQLite, filesystem cache and
   session, database queue. Create the admin user.
3. Admin → Nodes → Create. FQDN `192.168.0.158`, port 8080, SFTP 2022, SSL
   off. Add allocations `0.0.0.0`, ports 25565–25569.
4. Node → Configuration tab → copy the YAML into a file, then:

   ```bash
   kubectl -n pelican create secret generic pelican-wings-config \
     --from-file=config.yml=./config.yml
   ```

5. The Wings pod starts on its own within a minute. The node shows a green
   heart in the Panel.

The root README's secrets table gains a `pelican-wings-config` row and the
apps table a `pelican` row.

## Risks, accepted

- **Privileged dind.** A container escape from a game server lands in the
  dind container, which is privileged on the host. The players are friends
  and the servers are stock Minecraft; the trade is acceptable for a homelab.
  It is still the only privileged workload here that runs third-party code
  on demand, and the README says so.
- **Nested cgroup v2 and iptables inside a pod network namespace.** dind
  supports both and is widely run this way, but it is the first time on this
  cluster. If dockerd fails to start, the likely causes are cgroup nesting on
  Fedora's cgroup v2 or a missing `br_netfilter`. Both are host-side and
  would be documented, not worked around in git.
- **Pelican is beta.** Both images are `v1.0.0-betaNN`. Pin exact tags; bump
  deliberately.

## Verification

1. `kubectl kustomize apps/pelican` renders.
2. `kubectl -n pelican get pods` — panel Running and Ready; wings 2/2 after
   the Secret exists.
3. `kubectl -n pelican logs deploy/pelican-wings -c dind` shows dockerd
   listening; `-c wings` shows a successful Panel handshake.
4. Panel node page shows the green heart and reports the pod's memory/CPU.
5. Create a Vanilla Minecraft server on 25565. Install completes; console
   shows `Done`.
6. From a LAN client: connect to `192.168.0.158:25565`.
7. Delete the wings pod. It reschedules, possibly on another EliteDesk, and
   the world is still there — proves the Longhorn volumes carry the data.

## Out of scope

- **Public access.** Router port-forward is a later, manual step. Cloudflare,
  Tailscale and NetBird were all examined and set aside; NetBird's L4 reverse
  proxy would work without a client on the players' side but assigns the
  public port itself on shared cloud clusters (fixable with a Minecraft SRV
  record). Revisit if the port-forward turns out not to be wanted.
- **One Wings per EliteDesk.** A single Wings pod is enough until it is not.
  Scaling to three is a second Deployment with its own volumes and node
  entry, not a redesign.
- **Backups off-cluster.** Longhorn replicates against disk loss. Wings' own
  backup feature writes to `/var/lib/pelican/backups` on the same volume. An
  S3 backup target is a Panel setting, later.
- **A DNS name for the Panel URL.** Pi-hole local record, later.
