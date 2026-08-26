# Portfolio — design

Deploy [SilkePilon/portfolio](https://github.com/SilkePilon/portfolio) (Next.js 16 +
Payload CMS 3 + SQLite) to the cluster and serve it at `https://silkepilon.dev`
through the existing Cloudflare Tunnel.

## Image

`ghcr.io/silkepilon/portfolio:1.0.0`, built by the repo's release workflow for
`linux/amd64` only. The pod is therefore pinned with
`nodeSelector: kubernetes.io/arch: amd64` (the three HP minis); it is not pinned
to a specific node. Renovate bumps the tag on new releases.

## Runtime contract (from the Dockerfile)

- Port 3000, runs as `node` (UID 1000), non-root.
- `/data` holds everything that must survive restarts: `/data/payload.db`
  (SQLite) and `/data/media` (uploads). Defaults `DATABASE_URI` and `MEDIA_DIR`
  are baked into the image; we do not override them.
- `PAYLOAD_SECRET` required — read from Secret `portfolio-secret` (key
  `PAYLOAD_SECRET`), created once with `kubectl`, never committed.
- `SITE_URL=https://silkepilon.dev` (absolute links in metadata / share images).
- Health endpoint: `GET /api/access` (same one the image's HEALTHCHECK uses).

## Kubernetes resources (`apps/portfolio/`)

| File | Resource |
| --- | --- |
| `namespace.yaml` | Namespace `portfolio` |
| `pvc.yaml` | PVC `portfolio-data`, 5Gi, `longhorn`, RWO — Longhorn so the pod can move between the amd64 nodes |
| `deployment.yaml` | 1 replica, `Recreate` (RWO volume + single SQLite writer), `fsGroup`/`runAsUser` 1000, 256Mi–1Gi memory |
| `service.yaml` | ClusterIP `portfolio`, port 80 → `http` (3000) |
| `ingress.yaml` | Traefik Ingress for `silkepilon.dev` and `www.silkepilon.dev` |
| `kustomization.yaml` | Standard base with the `revisionHistoryLimit` patch |

Argo CD: `bootstrap/argocd/applications/portfolio.yaml`, copied from the template.

## Networking

Cloudflare Tunnel → Traefik → Service → Pod, like every other public app. Two
public-hostname entries must be added in the Zero Trust dashboard by hand
(`silkepilon.dev` and `www.silkepilon.dev`, both →
`http://traefik.kube-system.svc.cluster.local:80`). TLS terminates at Cloudflare.

## Out of scope

- Seeding content: the site falls back to its static content when the DB is
  empty; the first visit to `/admin` creates the admin user.
- Backups of the Longhorn volume (same story as every other app here).
