# Plane — design

Deploy [Plane](https://plane.so) (Community Edition) to the cluster and serve it
at `https://plane.silkepilon.dev` through the existing Cloudflare Tunnel.

## Shape

Helm-sourced Argo CD Application, like `tailscale` and `longhorn`: the chart is
rendered by Argo CD directly and there is no Kustomize base. Upstream ships
Plane as a chart (`plane/plane-ce`), and it renders nine workloads plus Traefik
`IngressRoute`/`Middleware` CRs — re-rendering that to static manifests on every
bump is not worth it.

- Chart: `https://helm.plane.so/` → `plane-ce` `1.8.0` (app `v1.4.1`).
- Namespace `plane`, release name `plane` (Services become `plane-api`,
  `plane-web`, `plane-pgdb`, …).
- Manifest: `bootstrap/argocd/applications/plane.yaml`. Docs: `apps/plane/README.md`.

## Topology (all from the chart)

| Workload | Kind | Role |
| --- | --- | --- |
| `plane-web` / `plane-space` / `plane-admin` | Deployment | Next.js front-ends (app, public "spaces", `/god-mode` admin) |
| `plane-api` | Deployment | Django API |
| `plane-worker` / `plane-beat-worker` | Deployment | Celery worker + scheduler |
| `plane-live` | Deployment | Collaborative editing websocket server |
| `plane-pgdb` | StatefulSet | PostgreSQL 15, 5Gi Longhorn |
| `plane-redis` | StatefulSet | Valkey, 256Mi Longhorn |
| `plane-rabbitmq` | StatefulSet | RabbitMQ, 1Gi Longhorn |
| `plane-minio` | StatefulSet | MinIO for uploads, 10Gi Longhorn |
| `plane-api-migrate-1` / `plane-minio-bucket-1` | Job | DB migrations / create `uploads` bucket |

All images are multi-arch, so nothing is pinned to the amd64 nodes.
`pullPolicy` is `IfNotPresent` (chart default is `Always`) — tags are immutable
and the Pis have small disks.

## Networking

Cloudflare Tunnel → Traefik → chart's `IngressRoute` → Services. The chart is
told `ssl.externalTermination: true`, so it renders `WEB_URL=https://…` while
binding the route to the plain `web` entrypoint (Cloudflare forwards cleartext).
One public hostname must be added by hand in Zero Trust:
`plane.silkepilon.dev` → `http://traefik.kube-system.svc.cluster.local:80`.

Upload body limit raised to 20 MiB on both the Traefik middleware and
`FILE_SIZE_LIMIT`.

## Secrets

The chart's defaults put passwords in values (and therefore in git). Every
`external_secrets.*_existingSecret` hook is pointed at one hand-made Secret,
`plane-secret`, holding all keys the chart would otherwise render:
`SECRET_KEY`, `LIVE_SERVER_SECRET_KEY`, `POSTGRES_{USER,PASSWORD,DB}`,
`RABBITMQ_DEFAULT_{USER,PASS}`, `DATABASE_URL`, `AMQP_URL`, `REDIS_URL`, and the
MinIO/S3 block (`USE_MINIO`, `MINIO_ROOT_*`, `AWS_*`, `FILE_SIZE_LIMIT`).
`env.requireExplicitSecrets: true` makes the render fail rather than fall back
to the chart's public example keys. The exact `kubectl create secret` is in
`apps/plane/README.md`.

`SECRET_KEY` encrypts instance configuration rows (SMTP password, OAuth client
secrets). Changing it silently breaks those — back it up.

## Argo CD specifics

- The chart stamps `timestamp: {{ now }}` on every pod template, so every
  refresh would show OutOfSync and every sync would roll every pod.
  `ignoreDifferences` on that annotation for Deployments and Jobs plus
  `RespectIgnoreDifferences=true` stops that.
- Job specs are immutable and the Job names embed `.Release.Revision`, which is
  always `1` under Argo CD. `api.annotations` and `minio.annotations` carry
  `argocd.argoproj.io/sync-options: Replace=true,Force=true` so a version bump
  deletes and recreates the migrate/bucket Jobs (re-running migrations, which
  is what an upgrade needs). Side effect: the same annotation lands on the
  `plane-api` Deployment and `plane-minio` StatefulSet, so those are recreated
  on real changes — roughly a minute of API downtime per upgrade. PVCs are
  untouched.

## Out of scope

- First-run setup happens in the browser at `/god-mode` (instance admin) and
  then `/` (first workspace).
- SMTP / OAuth: configured in god-mode after install.
- Backups of the Longhorn volumes (same story as every other app here).
