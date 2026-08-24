# Buzz Relay — Design

Date: 2026-08-24
Status: Approved

## Goal

Self-host the [Buzz](https://github.com/block/buzz) relay in the homelab cluster, reachable at
`https://relay.silkepilon.dev` (WebSocket `wss://relay.silkepilon.dev`), managed by Argo CD like
every other app in this repo.

Buzz is a Nostr-based workspace where humans and AI agents share rooms. The relay is a single
Rust binary serving WebSocket + REST + web UI, backed by Postgres, Redis, and S3-compatible
object storage.

## Decisions

| Question | Decision |
| --- | --- |
| Packaging | Plain Kustomize base, matching every other app here. The upstream Helm chart (`oci://ghcr.io/block/buzz/charts/buzz`) was rejected — it would be the only chart in the repo. |
| Object storage | In-cluster MinIO. Fully self-hosted; nothing leaves the cluster. Cloudflare R2 was rejected to keep media on-prem. |
| Owner identity | New Nostr keypair generated at install time. Relay stays closed (`BUZZ_REQUIRE_RELAY_MEMBERSHIP=true`). |
| Postgres | Own `postgres:17-alpine` Deployment in the namespace, matching `apps/twenty`. Upstream pins 17. |
| Redis | Kept, even at one relay replica. Upstream's production compose bundle always includes it, and dropping it would block any future scale-out. |
| Image tag | Pinned to `ghcr.io/block/buzz:0.2.1` — the current `latest`, built 2026-08-08. Multi-arch amd64 + arm64. |
| Bucket creation | An initContainer on the relay pod, not a Job. |
| Replicas | 1 for every workload. |

### Why the image tag is what it is

The relay has no semver release lane on GitHub Releases — only `desktop-v*` tags are published
there. GHCR carries `main` (rebuilt per commit), `latest`, and `sha-<7>` tags. `latest` currently
resolves to `org.opencontainers.image.version: 0.2.1`, and the tag `0.2.1` exists and resolves to
the same image, so that is what gets pinned. Renovate can track it as a normal semver Docker tag.

### Why the bucket initContainer, not a Job

The relay's S3 conformance probe (`BUZZ_GIT_CONFORMANCE_PROBE`) runs at startup and is fatal on
failure — readiness never opens if the bucket is missing, and the pod CrashLoopBackOffs with
growing backoff. A one-shot Job would create the bucket but also fight Argo CD's `selfHeal`, since
a completed Job is not a steady state. An idempotent initContainer (`mc mb --ignore-existing`)
both gates relay startup deterministically and re-runs harmlessly on every pod restart.

## Layout

```
apps/buzz/
  namespace.yaml
  pvc.yaml            # buzz-postgres-data, buzz-redis-data, buzz-minio-data, buzz-git-data
  postgres.yaml       # Deployment + Service (buzz-db)
  redis.yaml          # Deployment + Service (buzz-redis)
  minio.yaml          # Deployment + Service (buzz-minio)
  deployment.yaml     # relay + bucket initContainer
  service.yaml
  ingress.yaml
  kustomization.yaml
bootstrap/argocd/applications/buzz.yaml
```

Namespace: `buzz`. Every workload is `replicas: 1` with `strategy: Recreate`, because each holds a
ReadWriteOnce Longhorn volume.

## Workloads

### `buzz-db` — Postgres

`postgres:17-alpine`, port 5432, PVC `buzz-postgres-data` (10Gi, `longhorn`).

- `POSTGRES_USER=buzz`, `POSTGRES_DB=buzz`, `POSTGRES_PASSWORD` from `buzz-secret`.
- `PGDATA=/var/lib/postgresql/data/pgdata` so `lost+found` never collides with the data dir.
- `pg_isready` exec probes, following `apps/twenty/postgres.yaml`.
- Requests 100m/256Mi, limits 1 CPU/1Gi.

Service DNS: `buzz-db.buzz.svc.cluster.local`.

Schema migrations are embedded in the relay binary via `sqlx::migrate!` and run at relay startup
with `BUZZ_AUTO_MIGRATE=true`, serialized behind a Postgres advisory lock. There is no separate
migration Job.

### `buzz-redis` — Redis

`redis:8-alpine`, port 6379, PVC `buzz-redis-data` (2Gi, `longhorn`).

- Args: `--appendonly yes --requirepass $(REDIS_PASSWORD)`, password from `buzz-secret`.
- `redis-cli -a "$REDIS_PASSWORD" ping` exec probes.
- Requests 50m/64Mi, limits 500m/256Mi.

Upstream's compose bundle uses `redis:7-alpine`; 8 is used here to match `apps/twenty` and is
wire-compatible for the pubsub and presence use.

### `buzz-minio` — object storage

`minio/minio:RELEASE.2025-09-07T16-13-09Z`, `server /data --console-address :9001`, ports 9000
(API) and 9001 (console), PVC `buzz-minio-data` (20Gi, `longhorn`).

- `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` from `buzz-secret` keys `BUZZ_S3_ACCESS_KEY` /
  `BUZZ_S3_SECRET_KEY`.
- Probes hit `/minio/health/live` on 9000.
- Requests 100m/256Mi, limits 1 CPU/1Gi.
- Console is **not** exposed via Ingress. Reach it with `kubectl port-forward` when needed.

Addressing is path-style (`BUZZ_S3_ADDRESSING_STYLE=path`): cluster DNS resolves `buzz-minio`, not
arbitrary `<bucket>.buzz-minio` hostnames.

### `buzz` — the relay

`ghcr.io/block/buzz:0.2.1`.

Ports:

| Name | Port | Purpose |
| --- | --- | --- |
| `app` | 3000 | WebSocket + REST + web UI |
| `health` | 8080 | `/_liveness`, `/_readiness` |
| `metrics` | 9102 | Prometheus |

Volumes:

- `git-repos` → PVC `buzz-git-data` (10Gi, `longhorn`) at `/var/lib/buzz/git`.
- `git-pack-cache` → `emptyDir` with `sizeLimit: 7Gi` at `/var/cache/buzz/git-packs`.

Pod `securityContext`: `runAsNonRoot: true`, `runAsUser`/`runAsGroup`/`fsGroup` 65532,
`seccompProfile: RuntimeDefault` — the image is distroless-nonroot and the `fsGroup` is what makes
the git PVC writable on first mount. Container `securityContext`:
`allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `readOnlyRootFilesystem: false`
(git writes need a writable repo path).

initContainer `create-bucket` (`minio/mc:RELEASE.2025-08-13T08-35-41Z`) waits for MinIO to answer,
then `mc mb --ignore-existing local/buzz-media`. `MC_CONFIG_DIR=/tmp/.mc` so it needs no writable
home directory.

Probes: `startupProbe` and `livenessProbe` on `/_liveness`, `readinessProbe` on `/_readiness`, all
against the `health` port. `terminationGracePeriodSeconds: 60` — the relay hard-drains WebSockets
within 30s of SIGTERM.

Pod annotations `prometheus.io/scrape: "true"`, `prometheus.io/port: "9102"`,
`prometheus.io/path: "/metrics"`. The existing `kubernetes-pods` job in
`apps/monitoring/prometheus.yaml` discovers it with no config change.

Requests 500m/512Mi, limits 2 CPU/2Gi — the upstream chart's defaults.

#### Relay environment

Non-secret, literal in `deployment.yaml`:

```
BUZZ_BIND_ADDR=0.0.0.0:3000
BUZZ_HEALTH_PORT=8080
BUZZ_METRICS_PORT=9102
RELAY_URL=wss://relay.silkepilon.dev
BUZZ_MEDIA_BASE_URL=https://relay.silkepilon.dev/media
BUZZ_CORS_ORIGINS=https://relay.silkepilon.dev
RELAY_OWNER_PUBKEY=<64-char hex, filled in at install>
BUZZ_REQUIRE_AUTH_TOKEN=true
BUZZ_REQUIRE_RELAY_MEMBERSHIP=true
BUZZ_ALLOW_NIP_OA_AUTH=true
BUZZ_AUTO_MIGRATE=true
BUZZ_GIT_CONFORMANCE_PROBE=true
BUZZ_GIT_REPO_PATH=/var/lib/buzz/git
BUZZ_GIT_PACK_CACHE_PATH=/var/cache/buzz/git-packs
BUZZ_S3_ENDPOINT=http://buzz-minio:9000
BUZZ_S3_BUCKET=buzz-media
BUZZ_S3_REGION=us-east-1
BUZZ_S3_ADDRESSING_STYLE=path
RUST_LOG=buzz_relay=info,buzz_db=info,buzz_auth=info,buzz_pubsub=info,tower_http=info
TZ=Etc/UTC
```

`RELAY_OWNER_PUBKEY` is deliberately not prefixed with `BUZZ_` — that is upstream's naming, not a
typo. It is a public key, so it lives in the manifest, not the Secret.

From `buzz-secret`: `BUZZ_RELAY_PRIVATE_KEY`, `BUZZ_GIT_HOOK_HMAC_SECRET`, `DATABASE_URL`,
`REDIS_URL`, `BUZZ_S3_ACCESS_KEY`, `BUZZ_S3_SECRET_KEY`.

## Secret

One hand-created Secret `buzz-secret` in namespace `buzz`, matching the repo's convention of
never committing secrets. Keys:

| Key | Value | Consumed by |
| --- | --- | --- |
| `BUZZ_RELAY_PRIVATE_KEY` | 64 hex chars | relay — its Nostr identity |
| `BUZZ_GIT_HOOK_HMAC_SECRET` | 64 hex chars | relay |
| `DATABASE_URL` | `postgres://buzz:<pw>@buzz-db:5432/buzz` | relay |
| `REDIS_URL` | `redis://:<pw>@buzz-redis:6379` | relay |
| `POSTGRES_PASSWORD` | random | `buzz-db`, and embedded in `DATABASE_URL` |
| `REDIS_PASSWORD` | random | `buzz-redis`, and embedded in `REDIS_URL` |
| `BUZZ_S3_ACCESS_KEY` | random | relay + `buzz-minio` root user |
| `BUZZ_S3_SECRET_KEY` | random | relay + `buzz-minio` root password |

`POSTGRES_PASSWORD` and `REDIS_PASSWORD` are duplicated inside the URL keys. That coupling is
documented in the header comment of each manifest that consumes them: changing one without the
other breaks the relay's connection at startup, not at apply time.

Rotating `BUZZ_RELAY_PRIVATE_KEY` changes the relay's identity — federation peers stop
recognising it. Treat it as permanent.

## Key generation

`BUZZ_RELAY_PRIVATE_KEY`, `BUZZ_GIT_HOOK_HMAC_SECRET`, and the three passwords are
`openssl rand -hex 32`.

The owner keypair needs real secp256k1 x-only derivation, so it uses
[`nak`](https://github.com/fiatjaf/nak) (v0.20.6, single static binary,
`nak-v0.20.6-linux-amd64`):

```
nak key generate          # prints the private key (hex)
nak key public <privkey>  # prints the 64-char hex pubkey
```

The private key goes in a password manager and never touches the cluster — it is *your* identity
as operator, not the relay's. Only the pubkey is written into `deployment.yaml`.

## Networking

Traefik `Ingress` on `relay.silkepilon.dev` → Service `buzz` port 3000, `ingressClassName:
traefik`, entrypoint `web` — identical to `apps/n8n/ingress.yaml`.

One manual step outside the repo: in Cloudflare Zero Trust → Tunnel → Public Hostnames, add
subdomain `relay`, domain `silkepilon.dev`, service
`HTTP  http://traefik.kube-system.svc.cluster.local:80`.

WebSocket upgrades pass through Cloudflare Tunnel and Traefik without extra annotations. TLS is
terminated at Cloudflare, which is why the Ingress is plain HTTP.

## Storage

42Gi of new Longhorn claims (10 + 2 + 20 + 10), multiplied by the Longhorn replica count.

Back up, in order of how badly losing it hurts:

1. `BUZZ_RELAY_PRIVATE_KEY` — irreplaceable, it *is* the relay's identity.
2. The owner private key — held outside the cluster.
3. Postgres — the canonical event store.
4. MinIO `buzz-media` bucket — media blobs.
5. `buzz-git-data` — repo state served by the git endpoint.

## Verification

1. `kubectl kustomize apps/buzz` renders without error.
2. `kubectl apply -k apps/buzz --dry-run=server` passes.
3. All four Deployments reach `Available`.
4. `kubectl -n buzz exec deploy/buzz -- ...` or a port-forward returns 200 from `/_readiness` on
   8080.
5. `curl -H 'Accept: application/nostr+json' https://relay.silkepilon.dev` returns the NIP-11
   relay information document with the right pubkey.
6. A WebSocket connect to `wss://relay.silkepilon.dev` completes its handshake.
7. Prometheus shows a `buzz` target as up.

## Known risks

- **Frozen env contract.** These manifests encode upstream's environment interface as of
  2026-08-24. A later relay version that requires a new variable surfaces as a failed rollout,
  not as a chart-version bump. This is the accepted cost of hand-written Kustomize over the
  upstream chart.
- **Migration 0032** is a hard compatibility fence upstream — the relay verifies its trigger
  catalog before opening listeners. A fresh install clears it; a large version jump on an old
  database later may not.
- **Cloudflare WebSocket idle handling** is untested on this tunnel. The relay pings, so it should
  hold, but a dropped-connection symptom would point here first.
- **MinIO on Longhorn** is storage on top of replicated storage. Acceptable at homelab blob
  volume, wasteful at scale.
- **Single replica everywhere.** No HA. A node drain interrupts the relay until the Longhorn
  volume reattaches elsewhere.
