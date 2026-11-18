# Plane

[Plane](https://plane.so) Community Edition — issues, cycles, modules, pages.
Reachable at <https://plane.silkepilon.dev>.

The upstream `plane-ce` chart is rendered by the Argo CD Application at
[`bootstrap/argocd/applications/plane.yaml`](../../../bootstrap/argocd/applications/plane.yaml).
This folder is its second source: the ExternalSecret and RustFS (uploads).

## What the chart runs

| Workload | Role |
| --- | --- |
| `plane-web`, `plane-space`, `plane-admin` | Front-ends: app, public spaces, `/god-mode` |
| `plane-api`, `plane-worker`, `plane-beat-worker` | Django API, Celery worker, scheduler |
| `plane-live` | Collaborative-editing websocket server |
| `plane-pgdb`, `plane-redis`, `plane-rabbitmq` | Postgres 15, Valkey, RabbitMQ — all on Longhorn |
| `plane-rustfs` (git side, `rustfs.yaml`) | RustFS S3 store for uploads, on Longhorn; `/uploads` route in `uploads-route.yaml` |
| `plane-api-migrate-1`, `plane-rustfs-bucket` | One-shot Jobs: migrations, `uploads` bucket (PostSync hook) |

## Setup

### 1. Create the secret

Every credential lives in one Secret, `plane-secret`, which is **never
committed** — same rule as `twenty-secret`. It is synced from the Infisical
folder `/plane` (project `homelab`, environment `prod`) by `externalsecret.yaml`;
see [`apps/infra/external-secrets/README.md`](../../infra/external-secrets/README.md). The
chart's `external_secrets.*_existingSecret` hooks all point at it.

On a fresh install, add these keys to the Infisical folder `/plane`. Generate
three values with `openssl rand -hex 24` and call them PG_PASS, MQ_PASS and
S3_PASS:

| Key | Value |
| --- | --- |
| `SECRET_KEY` | `openssl rand -hex 32` |
| `LIVE_SERVER_SECRET_KEY` | `openssl rand -hex 32` |
| `POSTGRES_USER` | `plane` |
| `POSTGRES_PASSWORD` | PG_PASS |
| `POSTGRES_DB` | `plane` |
| `DATABASE_URL` | `postgresql://plane:<PG_PASS>@plane-pgdb.plane.svc.cluster.local:5432/plane` |
| `RABBITMQ_DEFAULT_USER` | `plane` |
| `RABBITMQ_DEFAULT_PASS` | MQ_PASS |
| `AMQP_URL` | `amqp://plane:<MQ_PASS>@plane-rabbitmq.plane.svc.cluster.local:5672/` |
| `REDIS_URL` | `redis://plane-redis.plane.svc.cluster.local:6379/` |
| `USE_MINIO` | `1` (Plane's "self-hosted S3" mode; RustFS behaves the same) |
| `AWS_ACCESS_KEY_ID` | any name, e.g. `plane` plus random hex |
| `AWS_SECRET_ACCESS_KEY` | S3_PASS (RustFS reads the same pair as its root login) |
| `AWS_S3_BUCKET_NAME` | `uploads` |
| `AWS_S3_ENDPOINT_URL` | `http://plane-rustfs.plane.svc.cluster.local:9000` |
| `FILE_SIZE_LIMIT` | `20971520` |
| `CELERY_WORKER_CONCURRENCY` | `4` |

`CELERY_WORKER_CONCURRENCY` is not a credential; it rides along in the Secret
because the chart has no other hook for extra env. Without it Celery forks one
child per CPU thread (16 on the HP minis) and the worker is OOM-killed at its
1000Mi limit.

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
  are immutable, so `api` carries
  `argocd.argoproj.io/sync-options: Replace=true,Force=true`. Every sync
  therefore deletes and recreates the migrate Job and, as a side effect, the
  `plane-api` Deployment. Expect about a minute of API downtime per sync.

- The admin image is stock nginx, so `GET /god-mode` without a trailing slash
  gets nginx's directory redirect, which includes the container port:
  `Location: https://plane.silkepilon.dev:3000/god-mode/`. Cloudflare does not
  proxy `:3000`, so the browser hangs. `extraObjects` adds a Traefik
  `redirectRegex` route for that exact path so Traefik answers
  `/god-mode` → `/god-mode/` itself.

- `minio.env.minio_endpoint_ssl: true` even though RustFS is plain http
  (the chart's MinIO is off, but this value still lands in the app env): the API builds presigned upload URLs from `request.scheme` unless
  this is set, and behind Cloudflare Django sees `http`, so browsers blocked
  `http://plane.silkepilon.dev/uploads` as mixed content. The setting only
  changes the URL scheme. ConfigMap changes do not restart pods (the chart's
  restart trigger is the `timestamp` annotation Argo CD ignores), so after
  changing it: `kubectl -n plane rollout restart deploy/plane-api-wl deploy/plane-worker-wl deploy/plane-beat-worker-wl`.

## Upgrading

Bump `targetRevision` (chart) and `planeVersion` (app) together in the
Application; the chart's `appVersion` says which app version it was tested
with (`helm search repo plane/plane-ce --versions`). Renovate tracks both.

## Notes

- Images are multi-arch, so pods schedule on the Pis too.
- Uploads used to live in the chart's MinIO. MinIO stopped publishing images,
  so on 2026-11-18 they moved to RustFS (byte-identical copy, verified). The
  RabbitMQ management UI (`:15672`) is not exposed; port-forward if needed.
