# Plane

[Plane](https://plane.so) Community Edition — issues, cycles, modules, pages.
Reachable at <https://plane.silkepilon.dev>.

Like `tailscale` and `longhorn` there is no Kustomize base: the upstream
`plane-ce` chart is rendered directly by the Argo CD Application at
[`bootstrap/argocd/applications/plane.yaml`](../../bootstrap/argocd/applications/plane.yaml).
Design notes: [docs/superpowers/specs/2026-08-29-plane-design.md](../../docs/superpowers/specs/2026-08-29-plane-design.md).

## What the chart runs

| Workload | Role |
| --- | --- |
| `plane-web`, `plane-space`, `plane-admin` | Front-ends: app, public spaces, `/god-mode` |
| `plane-api`, `plane-worker`, `plane-beat-worker` | Django API, Celery worker, scheduler |
| `plane-live` | Collaborative-editing websocket server |
| `plane-pgdb`, `plane-redis`, `plane-rabbitmq`, `plane-minio` | Postgres 15, Valkey, RabbitMQ, MinIO — all on Longhorn |
| `plane-api-migrate-1`, `plane-minio-bucket-1` | One-shot Jobs: migrations, `uploads` bucket |

## Setup

### 1. Create the secret

Every credential lives in one Secret, `plane-secret`, which is **never
committed** — same rule as `twenty-secret`. The chart's
`external_secrets.*_existingSecret` hooks all point at it.

```bash
kubectl create namespace plane

PG_PASS="$(openssl rand -hex 24)"
MQ_PASS="$(openssl rand -hex 24)"
MINIO_PASS="$(openssl rand -hex 24)"

kubectl create secret generic plane-secret -n plane \
  --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=LIVE_SERVER_SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=POSTGRES_USER=plane \
  --from-literal=POSTGRES_PASSWORD="$PG_PASS" \
  --from-literal=POSTGRES_DB=plane \
  --from-literal=DATABASE_URL="postgresql://plane:${PG_PASS}@plane-pgdb.plane.svc.cluster.local:5432/plane" \
  --from-literal=RABBITMQ_DEFAULT_USER=plane \
  --from-literal=RABBITMQ_DEFAULT_PASS="$MQ_PASS" \
  --from-literal=AMQP_URL="amqp://plane:${MQ_PASS}@plane-rabbitmq.plane.svc.cluster.local:5672/" \
  --from-literal=REDIS_URL="redis://plane-redis.plane.svc.cluster.local:6379/" \
  --from-literal=USE_MINIO=1 \
  --from-literal=MINIO_ROOT_USER=plane \
  --from-literal=MINIO_ROOT_PASSWORD="$MINIO_PASS" \
  --from-literal=AWS_ACCESS_KEY_ID=plane \
  --from-literal=AWS_SECRET_ACCESS_KEY="$MINIO_PASS" \
  --from-literal=AWS_S3_BUCKET_NAME=uploads \
  --from-literal=AWS_S3_ENDPOINT_URL="http://plane-minio.plane.svc.cluster.local:9000" \
  --from-literal=FILE_SIZE_LIMIT=20971520
```

> [!WARNING]
> Back up `SECRET_KEY`. It encrypts the instance-configuration rows (SMTP
> password, OAuth client secrets). Changing it makes them unreadable, silently.

Key names follow the chart's
[external secrets table](https://github.com/makeplane/helm-charts/blob/master/charts/plane-ce/README.md).

### 2. Cloudflare Tunnel

Zero Trust → Networks → Tunnels → `homelab` → Public Hostnames → add:

| Subdomain | Domain | Service |
| --- | --- | --- |
| `plane` | `silkepilon.dev` | `HTTP` `traefik.kube-system.svc.cluster.local:80` |

TLS is terminated by Cloudflare; the chart has `ssl.externalTermination: true`
so it renders `WEB_URL=https://plane.silkepilon.dev` while binding its
IngressRoute to Traefik's plain `web` entrypoint.

### 3. Let Argo CD sync

The app-of-apps picks up `bootstrap/argocd/applications/plane.yaml`. To nudge it:

```bash
kubectl -n argocd annotate application plane \
  argocd.argoproj.io/refresh=hard --overwrite
kubectl -n plane get pods -w
```

The `plane-api-migrate-1` Job must reach `Completed` before the API answers.

### 4. First run

1. Open <https://plane.silkepilon.dev/god-mode> and create the instance admin.
2. Open <https://plane.silkepilon.dev> and sign up — that account creates the
   first workspace.
3. SMTP, OAuth and sign-up policy live in god-mode.

## Argo CD quirks

- The chart puts `timestamp: {{ now }}` on every pod template. The Application
  ignores that path (plus `RespectIgnoreDifferences=true`), otherwise every
  refresh would be OutOfSync and every sync would roll every pod.
- Job names embed `.Release.Revision` (always `1` under Argo CD) and Job specs
  are immutable, so `api` and `minio` carry
  `argocd.argoproj.io/sync-options: Replace=true,Force=true`. A version bump
  therefore deletes and recreates the migrate/bucket Jobs — and, as a side
  effect, the `plane-api` Deployment and `plane-minio` StatefulSet. Expect about
  a minute of API downtime per upgrade. PVCs are kept.

## Upgrading

Bump `targetRevision` (chart) and `planeVersion` (app) together in the
Application; the chart's `appVersion` says which app version it was tested
with (`helm search repo plane/plane-ce --versions`). Renovate tracks both.

## Notes

- Images are multi-arch, so pods schedule on the Pis too.
- The MinIO console (`:9090`) and RabbitMQ management UI (`:15672`) are not
  exposed. Port-forward if needed:
  `kubectl -n plane port-forward svc/plane-minio 9090:9090`.
