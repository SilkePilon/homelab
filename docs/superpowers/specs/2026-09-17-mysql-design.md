# MySQL on the tailnet — design

A standalone MySQL 8.4 server for schoolwork, reachable from outside the LAN
as `mysql.<tailnet>.ts.net:3306`. It is not an app's backing store — it has no
consumer inside the cluster, and its only client is a laptop on the tailnet.

Deployed like every other app here: a Kustomize base at `apps/mysql/`, an Argo
CD Application at `bootstrap/argocd/applications/mysql.yaml`, and one Secret
created by hand.

## Why the tailnet and not the Cloudflare tunnel

The original ask was to expose MySQL through the existing Cloudflare tunnel.
That path does not work the way it does for the HTTP apps.

A tunnel public hostname carries HTTP/HTTPS. MySQL speaks its own binary
protocol over raw TCP, so a hostname cannot be handed to DBeaver or the `mysql`
CLI directly. Cloudflare offers two ways around it, and neither fits:

- **`cloudflared access tcp`** — the tunnel can carry arbitrary TCP, but only
  if the *client* machine runs `cloudflared access tcp --hostname db.example.com
  --url localhost:3306` first and then connects to `127.0.0.1:3306`. That is a
  binary to install on every machine that wants the database.
- **Cloudflare Spectrum** — genuine raw TCP on a public hostname, Enterprise
  plan only.

Tailscale proxies L4 TCP natively, the operator is already running in the
cluster, and the laptop that needs the database is already on the tailnet. So
`cloudflared` is not involved in this app at all: there is no Ingress, no
Traefik route, and no new public hostname.

This mirrors what Longhorn and Pi-hole already do — see the "Storage &
Networking" section of the README, and `apps/pihole/service.yaml`.

**Consequence, accepted:** a school machine with no Tailscale client gets
nothing. Access requires the tailnet.

## Access model

Root, reachable from anywhere on the tailnet, chosen deliberately over a
scoped non-root user so coursework can `CREATE DATABASE` freely.

This makes the tailnet ACL the real boundary. Any device that can reach
`tag:k8s` can also issue `DROP DATABASE`, with only the root password in
between. The MySQL grant table is not doing meaningful isolation work here.

`MYSQL_ROOT_HOST=%` is set explicitly. It is already the image default, but
leaving it implicit hides the single most consequential property of this
deployment.

**Prerequisite:** the tailnet policy must let the laptop reach `tag:k8s`. This
is the same grant Pi-hole and the Longhorn UI ride on, so if those are
reachable today, nothing new is needed.

## Manifests

`apps/mysql/`, copied from `apps/_template/` with `ingress.yaml` dropped.

### `namespace.yaml`

Namespace `mysql`, labelled `app.kubernetes.io/part-of: homelab`.

### `pvc.yaml`

`mysql-data`, `storageClassName: longhorn`, `ReadWriteOnce`, 10Gi — the same
class and size as `twenty-postgres-data`. Longhorn rather than `local-path` so
the volume is replicated and the pod is not pinned to the node that first
scheduled it.

### `deployment.yaml`

- Image `mysql:8.4` (LTS, supported to 2032). Verified multi-arch on Docker
  Hub: `amd64` and `arm64` manifests both present.
- `replicas: 1`, `strategy: Recreate` — ReadWriteOnce volume, one writer.
- Pinned to `kubernetes.io/arch: amd64`. The arm64 image exists, but the InnoDB
  buffer pool on a shared 4-8Gi Pi is a poor trade when three amd64 nodes have
  the headroom.
- Env: `MYSQL_ROOT_PASSWORD` from `mysql-secret`, `MYSQL_DATABASE=school`
  (created empty on first boot), `MYSQL_ROOT_HOST=%`.
- Volume `mysql-data` mounted at `/var/lib/mysql`. No `PGDATA`-style subpath
  is needed — the MySQL entrypoint tolerates a non-empty volume root and
  ignores `lost+found`.
- Resources: requests 100m / 256Mi, limits 1 CPU / 1Gi — matching `twenty-db`.
- Readiness and liveness probes both `mysqladmin ping -h 127.0.0.1` with the
  root password supplied from the environment, on the same 10s / 30s cadence
  `twenty-db` uses for `pg_isready`.

No `my.cnf` ConfigMap. Server defaults are adequate for coursework, and an
empty config file is unused surface that later readers have to check.

### `service.yaml`

ClusterIP on port 3306, annotated:

```yaml
tailscale.com/expose: "true"
tailscale.com/hostname: mysql
```

The operator creates a proxy StatefulSet in the `tailscale` namespace and
registers a tailnet device, exactly as it does for `pihole-web`. Removing the
annotations tears both down.

### `kustomization.yaml`

`namespace: mysql`, the four resources, the `app.kubernetes.io/part-of:
homelab` label with `includeSelectors: false`, and the `revisionHistoryLimit:
2` patches that every base in this repo carries.

### `bootstrap/argocd/applications/mysql.yaml`

Copied from `_template.yaml.tpl` with `<APP_NAME>` replaced by `mysql`.
`prune: true`, `selfHeal: true`, `CreateNamespace=true`, `ServerSideApply=true`
— unchanged from the template.

## Secret

Not committed, created once:

```bash
kubectl create namespace mysql
kubectl -n mysql create secret generic mysql-secret \
  --from-literal=MYSQL_ROOT_PASSWORD='...'
```

`MYSQL_ROOT_PASSWORD` is read only when the volume is empty. Changing the
Secret afterwards does not change the password — that takes an `ALTER USER`
against a running server.

The README's secrets table gains a `mysql-secret` row.

## Connecting

```bash
mysql -h mysql.<your-tailnet>.ts.net -P 3306 -u root -p school
```

Or in a GUI client: host `mysql.<your-tailnet>.ts.net`, port 3306, user `root`,
database `school`. MySQL 8.4 defaults to `caching_sha2_password`; current
clients and connectors handle it, but a sufficiently old PHP or JDBC driver
will need a connector upgrade rather than a server-side workaround — the
`mysql_native_password` plugin is not built into 8.4 by default.

## Verification

1. `kubectl -n mysql get pods` — pod Running, readiness passing.
2. `kubectl -n tailscale get pods` — a proxy StatefulSet for the `mysql`
   Service exists; a device named `mysql` appears in the tailnet admin panel.
3. From the laptop, on the tailnet: `mysql -h mysql.<tailnet>.ts.net -u root -p
   -e 'SHOW DATABASES;'` lists `school`.
4. Write, delete the pod, confirm the row survives the reschedule — proves the
   Longhorn volume is actually carrying the data.

## Out of scope

- **phpMyAdmin or any web UI.** Considered and declined; the CLI and a desktop
  client cover the need. If it is wanted later it is one more Deployment plus
  a second tailnet-annotated Service, no change to what is built here.
- **Backups.** Longhorn replicates against disk failure, which is not the same
  as recovering from a bad `DROP`. Given root-from-anywhere, a `mysqldump`
  CronJob is the obvious follow-up, but it is deliberately not in this change.
- **Public internet exposure.** No Ingress, no tunnel hostname.
