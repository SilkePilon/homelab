# Buzz Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the [block/buzz](https://github.com/block/buzz) Nostr relay on the homelab k3s cluster at `https://relay.silkepilon.dev`, managed by Argo CD.

**Architecture:** A plain Kustomize base at `apps/buzz/` holding four single-replica Deployments in namespace `buzz` — Postgres 17, Redis, MinIO, and the relay — each on its own ReadWriteOnce Longhorn volume. A Traefik `Ingress` fronts the relay; the existing Cloudflare Tunnel routes `relay.silkepilon.dev` to Traefik. One hand-created `buzz-secret` holds every credential; nothing secret is committed.

**Tech Stack:** k3s 1.35, Kustomize, Argo CD 3.4, Traefik, Longhorn, Cloudflare Tunnel, Prometheus (annotation-based scraping).

## Global Constraints

- Design spec: `docs/superpowers/specs/2026-08-24-buzz-relay-design.md`. It is authoritative; this plan implements it.
- Namespace: `buzz`. Every resource in `apps/buzz/` sets `namespace: buzz` explicitly.
- Every Deployment is `replicas: 1` with `strategy: { type: Recreate }` — all four hold ReadWriteOnce volumes.
- Every PVC uses `storageClassName: longhorn`.
- Image pins, exact:
  - relay: `ghcr.io/block/buzz:0.2.1`
  - Postgres: `postgres:17-alpine`
  - Redis: `redis:8-alpine`
  - MinIO: `minio/minio:RELEASE.2025-09-07T16-13-09Z`
  - MinIO client: `minio/mc:RELEASE.2025-08-13T08-35-41Z`
- Public hostname: `relay.silkepilon.dev`. `RELAY_URL=wss://relay.silkepilon.dev`.
- Secret name: `buzz-secret`, namespace `buzz`, created by hand with `kubectl`. Never committed.
- Every manifest carries a header comment in the style of `apps/n8n/` and `apps/twenty/` — what the file is, which secret keys it consumes, what must exist before it applies.
- Relay pod runs as UID/GID 65532 (`fsGroup: 65532`). The image is distroless-nonroot; without the `fsGroup` the git PVC mounts unwritable.
- Renovate manages image bumps via `.github/renovate.json5`, which already watches `/^apps/.+\.ya?ml$/`. No Renovate config change is needed.
- Commit messages use Conventional Commits (`feat:`, `docs:`, `chore:`) — the repo runs `:semanticCommits`.
- **Committing files under `apps/buzz/` deploys nothing.** Argo CD's root app watches `bootstrap/argocd/applications/` only. The stack goes live in Task 7, when the `Application` manifest lands. Tasks 2–6 are safe to commit and push freely.

---

### Task 1: Generate credentials and create the Secret

Nothing renders or deploys without this. The relay's owner pubkey produced here is a literal in Task 6's `deployment.yaml`, so this task must complete first.

**Files:**
- Create: none in the repo. This task produces cluster state and secrets you store outside git.

**Interfaces:**
- Produces:
  - Namespace `buzz` in the cluster.
  - Secret `buzz-secret` in namespace `buzz` with keys `BUZZ_RELAY_PRIVATE_KEY`, `BUZZ_GIT_HOOK_HMAC_SECRET`, `DATABASE_URL`, `REDIS_URL`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `BUZZ_S3_ACCESS_KEY`, `BUZZ_S3_SECRET_KEY`.
  - A 64-char lowercase hex owner pubkey, recorded for Task 6.

- [ ] **Step 1: Confirm the Secret does not already exist**

Run:

```bash
kubectl -n buzz get secret buzz-secret
```

Expected: `Error from server (NotFound): namespaces "buzz" not found`. If the Secret already exists, stop and ask before overwriting — regenerating `BUZZ_RELAY_PRIVATE_KEY` destroys the relay's identity.

- [ ] **Step 2: Install `nak` for Nostr key generation**

`nak` derives the secp256k1 x-only pubkey. `openssl` cannot do this. Single static binary, nothing installed system-wide:

```bash
curl -fsSL -o /tmp/nak https://github.com/fiatjaf/nak/releases/download/v0.20.6/nak-v0.20.6-linux-amd64
chmod +x /tmp/nak
/tmp/nak --version
```

Expected: a version string containing `0.20.6`.

- [ ] **Step 3: Generate the owner keypair**

```bash
OWNER_SEC=$(/tmp/nak key generate)
OWNER_PUB=$(/tmp/nak key public "$OWNER_SEC")
echo "OWNER PRIVATE KEY (password manager, never in the cluster): $OWNER_SEC"
echo "OWNER PUBKEY       (goes into deployment.yaml):              $OWNER_PUB"
/tmp/nak encode npub "$OWNER_PUB"
```

Expected: two 64-char lowercase hex strings and an `npub1…` string.

**Store `$OWNER_SEC` in your password manager now.** It is your identity as relay operator. It never enters the cluster — losing it means you cannot administer the relay as owner. Record `$OWNER_PUB`; Task 6 needs it.

- [ ] **Step 4: Generate the relay and service credentials**

```bash
RELAY_PRIVATE_KEY=$(openssl rand -hex 32)
GIT_HOOK_HMAC=$(openssl rand -hex 32)
PG_PASSWORD=$(openssl rand -hex 24)
REDIS_PASSWORD=$(openssl rand -hex 24)
S3_ACCESS_KEY=$(openssl rand -hex 16)
S3_SECRET_KEY=$(openssl rand -hex 32)
```

All values are hex, so they are URL-safe and need no escaping inside `DATABASE_URL` / `REDIS_URL`.

- [ ] **Step 5: Create the namespace and the Secret**

```bash
kubectl create namespace buzz

kubectl -n buzz create secret generic buzz-secret \
  --from-literal=BUZZ_RELAY_PRIVATE_KEY="$RELAY_PRIVATE_KEY" \
  --from-literal=BUZZ_GIT_HOOK_HMAC_SECRET="$GIT_HOOK_HMAC" \
  --from-literal=POSTGRES_PASSWORD="$PG_PASSWORD" \
  --from-literal=REDIS_PASSWORD="$REDIS_PASSWORD" \
  --from-literal=BUZZ_S3_ACCESS_KEY="$S3_ACCESS_KEY" \
  --from-literal=BUZZ_S3_SECRET_KEY="$S3_SECRET_KEY" \
  --from-literal=DATABASE_URL="postgres://buzz:${PG_PASSWORD}@buzz-db:5432/buzz" \
  --from-literal=REDIS_URL="redis://:${REDIS_PASSWORD}@buzz-redis:6379"
```

Expected: `namespace/buzz created` then `secret/buzz-secret created`.

- [ ] **Step 6: Verify every key is present**

```bash
kubectl -n buzz get secret buzz-secret -o go-template='{{range $k,$v := .data}}{{$k}}{{"\n"}}{{end}}' | sort
```

Expected, exactly these eight lines:

```
BUZZ_GIT_HOOK_HMAC_SECRET
BUZZ_RELAY_PRIVATE_KEY
BUZZ_S3_ACCESS_KEY
BUZZ_S3_SECRET_KEY
DATABASE_URL
POSTGRES_PASSWORD
REDIS_PASSWORD
REDIS_URL
```

- [ ] **Step 7: Back up the secret material**

Put these in your password manager, under one entry named "buzz relay":

- Owner private key (`$OWNER_SEC`) — irreplaceable, your operator identity.
- `BUZZ_RELAY_PRIVATE_KEY` — irreplaceable, the relay's Nostr identity. Rotating it makes federation peers treat the relay as a stranger.
- `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `BUZZ_S3_ACCESS_KEY`, `BUZZ_S3_SECRET_KEY` — recoverable by recreating the stack, but only by wiping data.
- Owner pubkey (`$OWNER_PUB`) — public, but you need it for Task 6.

Nothing to commit in this task.

---

### Task 2: Kustomize base skeleton — namespace and volumes

**Files:**
- Create: `apps/buzz/namespace.yaml`
- Create: `apps/buzz/pvc.yaml`
- Create: `apps/buzz/kustomization.yaml`

**Interfaces:**
- Consumes: nothing.
- Produces: PVC names `buzz-postgres-data`, `buzz-redis-data`, `buzz-minio-data`, `buzz-git-data`, all in namespace `buzz`, all `storageClassName: longhorn`. Tasks 3–6 mount these by name.

- [ ] **Step 1: Verify the render fails before the files exist**

Run:

```bash
kubectl kustomize apps/buzz
```

Expected: an error containing `apps/buzz` — the directory does not exist yet.

- [ ] **Step 2: Create `apps/buzz/namespace.yaml`**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: buzz
  labels:
    app.kubernetes.io/part-of: homelab
```

- [ ] **Step 3: Create `apps/buzz/pvc.yaml`**

```yaml
# ── Buzz volumes ─────────────────────────────────────────────────────────────
# Four ReadWriteOnce Longhorn claims. Longhorn replicates each one, so the
# real cluster cost is (size x longhorn replica count).
#
# Losing any of these is data loss:
#   buzz-postgres-data  the canonical event store — every message, reaction,
#                       workflow step and git event Buzz has ever seen
#   buzz-minio-data     media blobs (Blossom)
#   buzz-git-data       on-disk repo state served by the relay's git endpoint
#   buzz-redis-data     pubsub/presence only; rebuildable, but the appendonly
#                       file avoids a cold start
# ─────────────────────────────────────────────────────────────────────────────
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: buzz-postgres-data
  namespace: buzz
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn
  resources:
    requests:
      storage: 10Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: buzz-redis-data
  namespace: buzz
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn
  resources:
    requests:
      storage: 2Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: buzz-minio-data
  namespace: buzz
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn
  resources:
    requests:
      storage: 20Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: buzz-git-data
  namespace: buzz
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn
  resources:
    requests:
      storage: 10Gi
```

- [ ] **Step 4: Create `apps/buzz/kustomization.yaml`**

The `resources` list already names the files Tasks 3–6 add, so those tasks only create files. Kustomize fails on a missing resource, which is exactly the "test fails first" signal each later task starts from.

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

# Plain Kustomize base — consumed identically by Argo CD and Flux CD.
namespace: buzz

# ─────────────────────────────────────────────────────────────────────────────
# Buzz — a Nostr relay where humans and AI agents share rooms.
# https://github.com/block/buzz
#
# One relay binary (WebSocket + REST + web UI) backed by Postgres, Redis and
# an S3-compatible object store. All three run in this namespace.
#
# BEFORE applying this stack:
#
#  1. Create the namespace and the secret (NOT committed to git):
#
#       kubectl create namespace buzz
#
#       kubectl -n buzz create secret generic buzz-secret \
#         --from-literal=BUZZ_RELAY_PRIVATE_KEY="$(openssl rand -hex 32)" \
#         --from-literal=BUZZ_GIT_HOOK_HMAC_SECRET="$(openssl rand -hex 32)" \
#         --from-literal=POSTGRES_PASSWORD="<pg-pw>" \
#         --from-literal=REDIS_PASSWORD="<redis-pw>" \
#         --from-literal=BUZZ_S3_ACCESS_KEY="<s3-key>" \
#         --from-literal=BUZZ_S3_SECRET_KEY="<s3-secret>" \
#         --from-literal=DATABASE_URL="postgres://buzz:<pg-pw>@buzz-db:5432/buzz" \
#         --from-literal=REDIS_URL="redis://:<redis-pw>@buzz-redis:6379"
#
#     POSTGRES_PASSWORD and REDIS_PASSWORD are ALSO embedded in DATABASE_URL
#     and REDIS_URL. Change one without the other and the relay fails to
#     connect at startup — not at apply time.
#
#     BUZZ_RELAY_PRIVATE_KEY is the relay's Nostr identity. Rotating it makes
#     federation peers treat this relay as a different one. Back it up.
#
#  2. Set RELAY_OWNER_PUBKEY in deployment.yaml to your 64-char hex Nostr
#     pubkey (generate with `nak key generate` / `nak key public`). The relay
#     runs closed — only members the owner admits can join.
#
#  3. Add a Cloudflare Tunnel public hostname:
#       Subdomain: relay   Domain: silkepilon.dev
#       Service:   HTTP    http://traefik.kube-system.svc.cluster.local:80
#
#  4. Commit and push. Argo CD syncs it — no kubectl apply.
# ─────────────────────────────────────────────────────────────────────────────

resources:
  - namespace.yaml
  - pvc.yaml
  - postgres.yaml
  - redis.yaml
  - minio.yaml
  - service.yaml
  - deployment.yaml
  - ingress.yaml

labels:
  - pairs:
      app.kubernetes.io/part-of: homelab
    includeSelectors: false

# Keep only 2 old ReplicaSets/ControllerRevisions per workload instead of the
# Kubernetes default of 10. Without this the cluster accumulates dozens of
# empty ReplicaSets, and each retained revision pins its container image on the
# node — costly on the Pis, which have 28-58Gi disks.
patches:
  - target:
      kind: Deployment
    patch: |
      - op: add
        path: /spec/revisionHistoryLimit
        value: 2
  - target:
      kind: DaemonSet
    patch: |
      - op: add
        path: /spec/revisionHistoryLimit
        value: 2
```

- [ ] **Step 5: Verify the render fails on the not-yet-written files**

Run:

```bash
kubectl kustomize apps/buzz
```

Expected: FAIL with an error naming `postgres.yaml` — something like `accumulating resources: accumulation err='accumulating resources from 'postgres.yaml': ... no such file or directory'`. This confirms the kustomization is wired to the files Tasks 3–6 create.

- [ ] **Step 6: Verify the two finished files independently**

Render only what exists:

```bash
kubectl create --dry-run=client -o yaml -f apps/buzz/namespace.yaml >/dev/null && echo "namespace.yaml ok"
kubectl apply --dry-run=server -f apps/buzz/pvc.yaml
```

Expected: `namespace.yaml ok`, then four lines ending `(server dry run)`, one per PVC. The namespace exists from Task 1, so the server dry-run resolves.

- [ ] **Step 7: Commit**

```bash
git add apps/buzz/namespace.yaml apps/buzz/pvc.yaml apps/buzz/kustomization.yaml
git commit -m "feat(buzz): add namespace, volumes and kustomization"
```

---

### Task 3: Postgres

**Files:**
- Create: `apps/buzz/postgres.yaml` (Deployment + Service)

**Interfaces:**
- Consumes: PVC `buzz-postgres-data` (Task 2); Secret key `POSTGRES_PASSWORD` (Task 1).
- Produces: Service `buzz-db` on port 5432, resolvable in-namespace as `buzz-db`. This is the host inside `DATABASE_URL`. Database `buzz`, user `buzz`.

- [ ] **Step 1: Verify the render still fails for postgres.yaml**

Run:

```bash
kubectl kustomize apps/buzz 2>&1 | grep postgres.yaml
```

Expected: a line naming `postgres.yaml` as missing.

- [ ] **Step 2: Create `apps/buzz/postgres.yaml`**

```yaml
---
# ─────────────────────────────────────────────────────────────────────────────
# PostgreSQL 17 — Buzz's canonical event store, and its full-text search index.
# Service DNS: buzz-db.buzz.svc.cluster.local (referenced as `buzz-db` inside
# the namespace by DATABASE_URL in buzz-secret).
#
# Upstream pins 17 in deploy/compose/compose.yml. Do not downgrade.
#
# Schema migrations are embedded in the relay binary (sqlx::migrate!) and run
# at relay startup with BUZZ_AUTO_MIGRATE=true, serialized behind a Postgres
# advisory lock. There is no migration Job.
#
# POSTGRES_PASSWORD must match the password embedded in DATABASE_URL in
# buzz-secret. Changing one without the other fails at relay startup, not at
# apply time.
# ─────────────────────────────────────────────────────────────────────────────
apiVersion: apps/v1
kind: Deployment
metadata:
  name: buzz-db
  namespace: buzz
  labels:
    app.kubernetes.io/name: buzz-db
spec:
  replicas: 1
  # ReadWriteOnce PVC — only one writer at a time.
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: buzz-db
  template:
    metadata:
      labels:
        app.kubernetes.io/name: buzz-db
    spec:
      # postgres:17-alpine starts as root then drops to the postgres user via
      # its own entrypoint (it chowns PGDATA itself), so no securityContext.
      containers:
        - name: postgres
          image: postgres:17-alpine
          imagePullPolicy: IfNotPresent
          ports:
            - name: postgres
              containerPort: 5432
          env:
            - name: POSTGRES_USER
              value: buzz
            - name: POSTGRES_DB
              value: buzz
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: buzz-secret
                  key: POSTGRES_PASSWORD
            # Keep data out of the volume root so lost+found never collides.
            - name: PGDATA
              value: /var/lib/postgresql/data/pgdata
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: "1"
              memory: 1Gi
          readinessProbe:
            exec:
              command: ["pg_isready", "-U", "buzz", "-d", "buzz"]
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            exec:
              command: ["pg_isready", "-U", "buzz", "-d", "buzz"]
            initialDelaySeconds: 30
            periodSeconds: 30
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: buzz-postgres-data
---
apiVersion: v1
kind: Service
metadata:
  name: buzz-db
  namespace: buzz
  labels:
    app.kubernetes.io/name: buzz-db
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: buzz-db
  ports:
    - name: postgres
      port: 5432
      targetPort: postgres
```

- [ ] **Step 3: Verify it validates against the API server**

```bash
kubectl apply --dry-run=server -f apps/buzz/postgres.yaml
```

Expected:

```
deployment.apps/buzz-db created (server dry run)
service/buzz-db created (server dry run)
```

- [ ] **Step 4: Verify the secret reference resolves**

The dry-run does not check `secretKeyRef` targets, so check by hand:

```bash
kubectl -n buzz get secret buzz-secret -o jsonpath='{.data.POSTGRES_PASSWORD}' | head -c 8; echo
```

Expected: eight base64 characters, not an error. An empty result means Task 1 did not complete.

- [ ] **Step 5: Commit**

```bash
git add apps/buzz/postgres.yaml
git commit -m "feat(buzz): add postgres 17"
```

---

### Task 4: Redis

**Files:**
- Create: `apps/buzz/redis.yaml` (Deployment + Service)

**Interfaces:**
- Consumes: PVC `buzz-redis-data` (Task 2); Secret key `REDIS_PASSWORD` (Task 1).
- Produces: Service `buzz-redis` on port 6379, password-protected. This is the host inside `REDIS_URL`.

- [ ] **Step 1: Verify the render still fails for redis.yaml**

```bash
kubectl kustomize apps/buzz 2>&1 | grep redis.yaml
```

Expected: a line naming `redis.yaml` as missing.

- [ ] **Step 2: Create `apps/buzz/redis.yaml`**

The password reaches `redis-server` through a shell, because `--requirepass` needs the env var expanded. `command`/`args` with an explicit `sh -c` is the only way to get that in a container spec.

```yaml
---
# ─────────────────────────────────────────────────────────────────────────────
# Redis — Buzz uses it for pubsub fan-out and presence.
# Service DNS (in-namespace): buzz-redis -> REDIS_URL=redis://:<pw>@buzz-redis:6379
#
# Upstream's compose bundle pins redis:7-alpine; 8 is used here to match
# apps/twenty and is wire-compatible for pubsub and presence.
#
# appendonly yes gives presence state a warm restart. The PVC is small because
# nothing durable lives here — the event log is Postgres.
#
# REDIS_PASSWORD must match the password embedded in REDIS_URL in buzz-secret.
# ─────────────────────────────────────────────────────────────────────────────
apiVersion: apps/v1
kind: Deployment
metadata:
  name: buzz-redis
  namespace: buzz
  labels:
    app.kubernetes.io/name: buzz-redis
spec:
  replicas: 1
  # ReadWriteOnce PVC — only one writer at a time.
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: buzz-redis
  template:
    metadata:
      labels:
        app.kubernetes.io/name: buzz-redis
    spec:
      containers:
        - name: redis
          image: redis:8-alpine
          imagePullPolicy: IfNotPresent
          # sh -c so $REDIS_PASSWORD is expanded; --requirepass takes a literal.
          command: ["sh", "-c"]
          args:
            - exec redis-server --appendonly yes --requirepass "$REDIS_PASSWORD"
          env:
            - name: REDIS_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: buzz-secret
                  key: REDIS_PASSWORD
          ports:
            - name: redis
              containerPort: 6379
          volumeMounts:
            - name: data
              mountPath: /data
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 500m
              memory: 256Mi
          readinessProbe:
            exec:
              command: ["sh", "-c", 'redis-cli -a "$REDIS_PASSWORD" ping | grep -q PONG']
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            exec:
              command: ["sh", "-c", 'redis-cli -a "$REDIS_PASSWORD" ping | grep -q PONG']
            initialDelaySeconds: 15
            periodSeconds: 30
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: buzz-redis-data
---
apiVersion: v1
kind: Service
metadata:
  name: buzz-redis
  namespace: buzz
  labels:
    app.kubernetes.io/name: buzz-redis
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: buzz-redis
  ports:
    - name: redis
      port: 6379
      targetPort: redis
```

- [ ] **Step 3: Verify it validates against the API server**

```bash
kubectl apply --dry-run=server -f apps/buzz/redis.yaml
```

Expected:

```
deployment.apps/buzz-redis created (server dry run)
service/buzz-redis created (server dry run)
```

- [ ] **Step 4: Commit**

```bash
git add apps/buzz/redis.yaml
git commit -m "feat(buzz): add redis"
```

---

### Task 5: MinIO

**Files:**
- Create: `apps/buzz/minio.yaml` (Deployment + Service)

**Interfaces:**
- Consumes: PVC `buzz-minio-data` (Task 2); Secret keys `BUZZ_S3_ACCESS_KEY`, `BUZZ_S3_SECRET_KEY` (Task 1).
- Produces: Service `buzz-minio` with port 9000 named `api` and port 9001 named `console`. The relay reaches it at `http://buzz-minio:9000`. Bucket `buzz-media` is *not* created here — Task 6's initContainer does that.

- [ ] **Step 1: Verify the render still fails for minio.yaml**

```bash
kubectl kustomize apps/buzz 2>&1 | grep minio.yaml
```

Expected: a line naming `minio.yaml` as missing.

- [ ] **Step 2: Create `apps/buzz/minio.yaml`**

```yaml
---
# ─────────────────────────────────────────────────────────────────────────────
# MinIO — S3-compatible object storage for Buzz media blobs (Blossom) and the
# git object store. Not optional: the relay probes S3 at startup and the probe
# is fatal, so a broken MinIO means the relay never becomes ready.
#
# Addressing is path-style (BUZZ_S3_ADDRESSING_STYLE=path in deployment.yaml).
# Cluster DNS resolves `buzz-minio`, not `<bucket>.buzz-minio`, so virtual-host
# style cannot work here.
#
# The bucket `buzz-media` is created by the relay Pod's create-bucket
# initContainer (deployment.yaml), not here — a Job would fight Argo CD's
# selfHeal, since a completed Job is not a steady state.
#
# The console (9001) is deliberately NOT exposed through the Ingress. Reach it
# with:
#   kubectl -n buzz port-forward svc/buzz-minio 9001:9001
# and log in with BUZZ_S3_ACCESS_KEY / BUZZ_S3_SECRET_KEY from buzz-secret.
# ─────────────────────────────────────────────────────────────────────────────
apiVersion: apps/v1
kind: Deployment
metadata:
  name: buzz-minio
  namespace: buzz
  labels:
    app.kubernetes.io/name: buzz-minio
spec:
  replicas: 1
  # ReadWriteOnce PVC — only one writer at a time.
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: buzz-minio
  template:
    metadata:
      labels:
        app.kubernetes.io/name: buzz-minio
    spec:
      containers:
        - name: minio
          image: minio/minio:RELEASE.2025-09-07T16-13-09Z
          imagePullPolicy: IfNotPresent
          args: ["server", "/data", "--console-address", ":9001"]
          env:
            - name: MINIO_ROOT_USER
              valueFrom:
                secretKeyRef:
                  name: buzz-secret
                  key: BUZZ_S3_ACCESS_KEY
            - name: MINIO_ROOT_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: buzz-secret
                  key: BUZZ_S3_SECRET_KEY
          ports:
            - name: api
              containerPort: 9000
            - name: console
              containerPort: 9001
          volumeMounts:
            - name: data
              mountPath: /data
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: "1"
              memory: 1Gi
          readinessProbe:
            httpGet:
              path: /minio/health/ready
              port: api
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /minio/health/live
              port: api
            initialDelaySeconds: 30
            periodSeconds: 30
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: buzz-minio-data
---
apiVersion: v1
kind: Service
metadata:
  name: buzz-minio
  namespace: buzz
  labels:
    app.kubernetes.io/name: buzz-minio
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: buzz-minio
  ports:
    - name: api
      port: 9000
      targetPort: api
    - name: console
      port: 9001
      targetPort: console
```

- [ ] **Step 3: Verify it validates against the API server**

```bash
kubectl apply --dry-run=server -f apps/buzz/minio.yaml
```

Expected:

```
deployment.apps/buzz-minio created (server dry run)
service/buzz-minio created (server dry run)
```

- [ ] **Step 4: Commit**

```bash
git add apps/buzz/minio.yaml
git commit -m "feat(buzz): add minio object storage"
```

---

### Task 6: The relay — Deployment, Service, Ingress

The largest task. It needs the owner pubkey from Task 1 Step 3.

**Files:**
- Create: `apps/buzz/deployment.yaml`
- Create: `apps/buzz/service.yaml`
- Create: `apps/buzz/ingress.yaml`

**Interfaces:**
- Consumes: Services `buzz-db` (Task 3), `buzz-redis` (Task 4), `buzz-minio` (Task 5); PVC `buzz-git-data` (Task 2); Secret keys `BUZZ_RELAY_PRIVATE_KEY`, `BUZZ_GIT_HOOK_HMAC_SECRET`, `DATABASE_URL`, `REDIS_URL`, `BUZZ_S3_ACCESS_KEY`, `BUZZ_S3_SECRET_KEY` (Task 1); the owner pubkey from Task 1.
- Produces: Service `buzz` with port 3000 named `http` and port 9102 named `metrics`; `Ingress` `buzz` on host `relay.silkepilon.dev` routing to `buzz:http`.

- [ ] **Step 1: Verify the render still fails for the three files**

```bash
kubectl kustomize apps/buzz 2>&1 | grep -E 'service.yaml|deployment.yaml|ingress.yaml'
```

Expected: a line naming `service.yaml` as missing (Kustomize stops at the first one).

- [ ] **Step 2: Create `apps/buzz/service.yaml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: buzz
  namespace: buzz
  labels:
    app.kubernetes.io/name: buzz
spec:
  # ClusterIP — public access arrives via Cloudflare Tunnel -> Traefik Ingress.
  type: ClusterIP
  selector:
    app.kubernetes.io/name: buzz
  ports:
    - name: http
      port: 3000
      targetPort: http
    - name: metrics
      port: 9102
      targetPort: metrics
```

- [ ] **Step 3: Create `apps/buzz/deployment.yaml`**

Replace `REPLACE_WITH_OWNER_PUBKEY` with the 64-char hex pubkey from Task 1 Step 3 before saving. Everything else is final.

```yaml
---
# ─────────────────────────────────────────────────────────────────────────────
# Buzz relay — https://github.com/block/buzz
# Image: ghcr.io/block/buzz:0.2.1   (multi-arch: amd64 + arm64)
# Ports: 3000 WebSocket + REST + web UI
#        8080 health (/_liveness, /_readiness)
#        9102 Prometheus metrics
# Data:  /var/lib/buzz/git   (PVC buzz-git-data)
#
# The relay runs closed: RELAY_OWNER_PUBKEY below is the operator identity and
# BUZZ_REQUIRE_RELAY_MEMBERSHIP=true means only members the owner admits can
# join. The pubkey is public — that is why it is a literal here and not in the
# Secret. Its matching PRIVATE key must never enter the cluster.
#
# RELAY_OWNER_PUBKEY is deliberately not prefixed with BUZZ_. That is upstream's
# naming, not a typo here.
#
# Secret keys consumed from buzz-secret:
#   BUZZ_RELAY_PRIVATE_KEY     the relay's own Nostr identity — back it up,
#                              rotating it makes federation peers see a
#                              different relay
#   BUZZ_GIT_HOOK_HMAC_SECRET  git hook signing
#   DATABASE_URL               postgres://buzz:<pw>@buzz-db:5432/buzz
#   REDIS_URL                  redis://:<pw>@buzz-redis:6379
#   BUZZ_S3_ACCESS_KEY         also MinIO's root user
#   BUZZ_S3_SECRET_KEY         also MinIO's root password
#
# Two initContainers exist to avoid CrashLoopBackOff, not for convenience: the
# relay's S3 conformance probe and its DB connection are both startup-fatal, so
# racing a cold Postgres or a missing bucket costs minutes of backoff.
# ─────────────────────────────────────────────────────────────────────────────
apiVersion: apps/v1
kind: Deployment
metadata:
  name: buzz
  namespace: buzz
  labels:
    app.kubernetes.io/name: buzz
spec:
  replicas: 1
  # ReadWriteOnce git PVC — only one writer at a time.
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: buzz
  template:
    metadata:
      labels:
        app.kubernetes.io/name: buzz
      annotations:
        # Picked up by the kubernetes-pods job in apps/monitoring/prometheus.yaml.
        prometheus.io/scrape: "true"
        prometheus.io/port: "9102"
        prometheus.io/path: "/metrics"
    spec:
      # The image is distroless-nonroot (UID 65532). fsGroup is what makes the
      # git PVC writable on first mount.
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
        runAsGroup: 65532
        fsGroup: 65532
        seccompProfile:
          type: RuntimeDefault
      # The relay hard-drains WebSockets within 30s of SIGTERM.
      terminationGracePeriodSeconds: 60
      initContainers:
        # Postgres is startup-fatal for the relay. Waiting here turns a
        # crash-loop with growing backoff into a quiet hold.
        - name: wait-for-postgres
          image: postgres:17-alpine
          imagePullPolicy: IfNotPresent
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: [ALL]
          command: ["sh", "-c"]
          args:
            - |
              until pg_isready -h buzz-db -p 5432 -U buzz; do
                echo "waiting for buzz-db..."; sleep 2
              done
          resources:
            requests:
              cpu: 10m
              memory: 16Mi
            limits:
              cpu: 100m
              memory: 64Mi
        # Creates the media bucket if it is missing. Idempotent, so it re-runs
        # harmlessly on every restart — unlike a Job, which Argo CD's selfHeal
        # would keep trying to reconcile.
        - name: create-bucket
          image: minio/mc:RELEASE.2025-08-13T08-35-41Z
          imagePullPolicy: IfNotPresent
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: [ALL]
          env:
            # mc writes its config to $MC_CONFIG_DIR; the default is $HOME,
            # which UID 65532 does not own.
            - name: MC_CONFIG_DIR
              value: /tmp/.mc
            - name: S3_ACCESS_KEY
              valueFrom:
                secretKeyRef:
                  name: buzz-secret
                  key: BUZZ_S3_ACCESS_KEY
            - name: S3_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: buzz-secret
                  key: BUZZ_S3_SECRET_KEY
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -e
              until mc alias set local http://buzz-minio:9000 "$S3_ACCESS_KEY" "$S3_SECRET_KEY" >/dev/null 2>&1; do
                echo "waiting for buzz-minio..."; sleep 2
              done
              mc mb --ignore-existing local/buzz-media
              mc anonymous set none local/buzz-media
              echo "bucket buzz-media present"
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              cpu: 200m
              memory: 128Mi
      containers:
        - name: relay
          image: ghcr.io/block/buzz:0.2.1
          imagePullPolicy: IfNotPresent
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: [ALL]
            # git writes need a writable repo path
            readOnlyRootFilesystem: false
          ports:
            - name: http
              containerPort: 3000
            - name: health
              containerPort: 8080
            - name: metrics
              containerPort: 9102
          env:
            - name: TZ
              value: "Etc/UTC"

            # ── Networking ───────────────────────────────────────────────────
            - name: BUZZ_BIND_ADDR
              value: "0.0.0.0:3000"
            - name: BUZZ_HEALTH_PORT
              value: "8080"
            - name: BUZZ_METRICS_PORT
              value: "9102"
            # Advertised in the NIP-11 document and used for NIP-42 auth
            # challenges. Must match the public hostname in ingress.yaml.
            - name: RELAY_URL
              value: "wss://relay.silkepilon.dev"
            - name: BUZZ_MEDIA_BASE_URL
              value: "https://relay.silkepilon.dev/media"
            - name: BUZZ_CORS_ORIGINS
              value: "https://relay.silkepilon.dev"

            # ── Owner / access model ─────────────────────────────────────────
            - name: RELAY_OWNER_PUBKEY
              value: "REPLACE_WITH_OWNER_PUBKEY"
            - name: BUZZ_REQUIRE_AUTH_TOKEN
              value: "true"
            - name: BUZZ_REQUIRE_RELAY_MEMBERSHIP
              value: "true"
            - name: BUZZ_ALLOW_NIP_OA_AUTH
              value: "true"

            # ── Migrations ───────────────────────────────────────────────────
            # Embedded in the binary via sqlx::migrate!, serialized behind a
            # Postgres advisory lock.
            - name: BUZZ_AUTO_MIGRATE
              value: "true"

            # ── Git ──────────────────────────────────────────────────────────
            - name: BUZZ_GIT_REPO_PATH
              value: "/var/lib/buzz/git"
            - name: BUZZ_GIT_PACK_CACHE_PATH
              value: "/var/cache/buzz/git-packs"
            # Startup-fatal S3 reachability check. Left on deliberately: a
            # silent misconfiguration would otherwise surface on first upload.
            - name: BUZZ_GIT_CONFORMANCE_PROBE
              value: "true"

            # ── S3 (non-secret) ──────────────────────────────────────────────
            - name: BUZZ_S3_ENDPOINT
              value: "http://buzz-minio:9000"
            - name: BUZZ_S3_BUCKET
              value: "buzz-media"
            - name: BUZZ_S3_REGION
              value: "us-east-1"
            # Cluster DNS resolves buzz-minio, not <bucket>.buzz-minio.
            - name: BUZZ_S3_ADDRESSING_STYLE
              value: "path"

            # ── Misc ─────────────────────────────────────────────────────────
            # Safe at one replica; upstream disables it above one until an SFU
            # exists.
            - name: BUZZ_HUDDLE_AUDIO_AVAILABLE
              value: "true"
            - name: RUST_LOG
              value: "buzz_relay=info,buzz_db=info,buzz_auth=info,buzz_pubsub=info,tower_http=info"

            # ── Secrets ──────────────────────────────────────────────────────
            - name: BUZZ_RELAY_PRIVATE_KEY
              valueFrom:
                secretKeyRef:
                  name: buzz-secret
                  key: BUZZ_RELAY_PRIVATE_KEY
            - name: BUZZ_GIT_HOOK_HMAC_SECRET
              valueFrom:
                secretKeyRef:
                  name: buzz-secret
                  key: BUZZ_GIT_HOOK_HMAC_SECRET
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: buzz-secret
                  key: DATABASE_URL
            - name: REDIS_URL
              valueFrom:
                secretKeyRef:
                  name: buzz-secret
                  key: REDIS_URL
            - name: BUZZ_S3_ACCESS_KEY
              valueFrom:
                secretKeyRef:
                  name: buzz-secret
                  key: BUZZ_S3_ACCESS_KEY
            - name: BUZZ_S3_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: buzz-secret
                  key: BUZZ_S3_SECRET_KEY
          volumeMounts:
            - name: git-repos
              mountPath: /var/lib/buzz/git
            - name: git-pack-cache
              mountPath: /var/cache/buzz/git-packs
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
            limits:
              cpu: "2"
              memory: 2Gi
          startupProbe:
            httpGet:
              path: /_liveness
              port: health
            # 30 x 5s = 150s for first-boot migrations before giving up.
            initialDelaySeconds: 10
            periodSeconds: 5
            failureThreshold: 30
          readinessProbe:
            httpGet:
              path: /_readiness
              port: health
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 3
          livenessProbe:
            httpGet:
              path: /_liveness
              port: health
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 3
      volumes:
        - name: git-repos
          persistentVolumeClaim:
            claimName: buzz-git-data
        - name: git-pack-cache
          emptyDir:
            sizeLimit: 7Gi
```

- [ ] **Step 4: Confirm the placeholder is gone**

```bash
grep -n REPLACE_WITH_OWNER_PUBKEY apps/buzz/deployment.yaml
```

Expected: no output, exit code 1. If it prints a line, the pubkey was not filled in — the relay would refuse to start in closed mode.

Then confirm the value is well-formed:

```bash
grep -A1 'RELAY_OWNER_PUBKEY' apps/buzz/deployment.yaml | grep -oE '[0-9a-f]{64}'
```

Expected: the 64-char hex pubkey from Task 1. No output means it is malformed — uppercase, an `npub1…`, or the wrong length.

- [ ] **Step 5: Create `apps/buzz/ingress.yaml`**

```yaml
# ── Buzz Ingress ─────────────────────────────────────────────────────────────
# Traffic flow: Cloudflare Tunnel -> Traefik -> buzz Service -> relay Pod.
# TLS terminates at Cloudflare, so this is a plain HTTP entrypoint.
#
# WebSocket upgrades pass through both hops with no extra annotations.
#
# In the Cloudflare Zero Trust dashboard add:
#   Public Hostname -> Subdomain: relay   Domain: silkepilon.dev
#   Service         -> HTTP      http://traefik.kube-system.svc.cluster.local:80
#
# If you change the host here, also change RELAY_URL, BUZZ_MEDIA_BASE_URL and
# BUZZ_CORS_ORIGINS in deployment.yaml — the relay advertises RELAY_URL in its
# NIP-11 document and uses it for NIP-42 auth challenges, so a mismatch breaks
# client authentication rather than failing loudly at startup.
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: buzz
  namespace: buzz
  annotations:
    traefik.ingress.kubernetes.io/router.entrypoints: web
spec:
  ingressClassName: traefik
  rules:
    - host: relay.silkepilon.dev
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: buzz
                port:
                  name: http
```

- [ ] **Step 6: Verify the whole base renders**

```bash
kubectl kustomize apps/buzz > /tmp/buzz-render.yaml && echo "render ok"
grep '^kind:' /tmp/buzz-render.yaml | sort | uniq -c
```

Expected `render ok`, then exactly 14 objects:

```
      4 kind: Deployment
      1 kind: Ingress
      1 kind: Namespace
      4 kind: PersistentVolumeClaim
      4 kind: Service
```

- [ ] **Step 7: Verify the whole base validates against the API server**

```bash
kubectl apply -k apps/buzz --dry-run=server
```

Expected: 14 lines ending `(server dry run)`, no errors. This is the last gate before Argo CD gets involved.

- [ ] **Step 8: Confirm `revisionHistoryLimit` was patched in**

```bash
grep -c 'revisionHistoryLimit: 2' /tmp/buzz-render.yaml
```

Expected: `4`, one per Deployment. A `0` means the `patches` block in `kustomization.yaml` did not match.

- [ ] **Step 9: Commit**

```bash
git add apps/buzz/service.yaml apps/buzz/deployment.yaml apps/buzz/ingress.yaml
git commit -m "feat(buzz): add relay deployment, service and ingress"
```

---

### Task 7: Argo CD Application — go live

This is the task that actually deploys. Everything before it was inert.

**Files:**
- Create: `bootstrap/argocd/applications/buzz.yaml`

**Interfaces:**
- Consumes: the whole `apps/buzz/` base (Tasks 2–6); Secret `buzz-secret` (Task 1).
- Produces: a running stack in namespace `buzz`.

- [ ] **Step 1: Verify Argo CD does not know about buzz yet**

```bash
kubectl -n argocd get application buzz
```

Expected: `Error from server (NotFound): applications.argoproj.io "buzz" not found`.

- [ ] **Step 2: Create `bootstrap/argocd/applications/buzz.yaml`**

Copied from `bootstrap/argocd/applications/twenty.yaml`, which is the established shape for an app with its own in-namespace datastores.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: buzz
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  source:
    repoURL: https://github.com/SilkePilon/homelab
    targetRevision: main
    path: apps/buzz
  destination:
    server: https://kubernetes.default.svc
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
```

- [ ] **Step 3: Validate it before pushing**

```bash
kubectl apply --dry-run=server -f bootstrap/argocd/applications/buzz.yaml
```

Expected: `application.argoproj.io/buzz created (server dry run)`.

- [ ] **Step 4: Commit and push**

Pushing is the deploy. The root app-of-apps polls `bootstrap/argocd/applications/` and creates the `buzz` Application, which then syncs `apps/buzz/`.

```bash
git add bootstrap/argocd/applications/buzz.yaml
git commit -m "feat(buzz): add argo cd application"
git push
```

- [ ] **Step 5: Wait for Argo CD to pick it up**

Argo CD polls roughly every three minutes. To skip the wait:

```bash
kubectl -n argocd patch application root --type merge -p '{"operation":{"sync":{}}}'
```

Then watch:

```bash
kubectl -n argocd get application buzz -w
```

Expected: the `buzz` Application appears and moves to `Synced` / `Progressing`, then `Synced` / `Healthy`. Press Ctrl-C once it is Healthy.

- [ ] **Step 6: Verify all four workloads rolled out**

```bash
for d in buzz-db buzz-redis buzz-minio buzz; do
  kubectl -n buzz rollout status deploy/$d --timeout=300s
done
```

Expected: `deployment "<name>" successfully rolled out` four times.

If `buzz` stalls, read the initContainers first — they are where dependency problems surface:

```bash
kubectl -n buzz logs deploy/buzz -c wait-for-postgres
kubectl -n buzz logs deploy/buzz -c create-bucket
kubectl -n buzz logs deploy/buzz -c relay
```

- [ ] **Step 7: Verify the bucket exists**

```bash
kubectl -n buzz logs deploy/buzz -c create-bucket | tail -1
```

Expected: `bucket buzz-media present`.

- [ ] **Step 8: Verify readiness from inside the cluster**

```bash
kubectl -n buzz port-forward deploy/buzz 18080:8080 &
sleep 3
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18080/_readiness
kill %1
```

Expected: `200`. Anything else means the relay started but a dependency check failed — Postgres, Redis, or the S3 conformance probe. `kubectl -n buzz logs deploy/buzz -c relay` names which.

- [ ] **Step 9: Verify migrations ran**

```bash
kubectl -n buzz exec deploy/buzz-db -- psql -U buzz -d buzz -c '\dt' | head -20
```

Expected: a table listing, not `Did not find any relations`. The relay creates the schema on first boot.

---

### Task 8: Cloudflare Tunnel hostname and external verification

**Files:**
- Modify: none. This task is a Cloudflare dashboard change plus verification.

**Interfaces:**
- Consumes: the running Ingress from Task 7.
- Produces: `https://relay.silkepilon.dev` resolving publicly.

- [ ] **Step 1: Verify the hostname is not yet routed**

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://relay.silkepilon.dev
```

Expected: a Cloudflare error code (`530`, `1016`, or a DNS failure). A `200` here means the hostname is already configured — skip to Step 3.

- [ ] **Step 2: Add the public hostname in Cloudflare**

In the Cloudflare Zero Trust dashboard → Networks → Tunnels → your tunnel → Public Hostname → Add a public hostname:

- Subdomain: `relay`
- Domain: `silkepilon.dev`
- Path: empty
- Type: `HTTP`
- URL: `traefik.kube-system.svc.cluster.local:80`

Save. Cloudflare creates the DNS record itself.

- [ ] **Step 3: Verify the NIP-11 relay information document**

This is the single most informative external check — it proves the tunnel, Traefik, the Ingress, and the relay's own config all agree.

```bash
curl -fsS -H 'Accept: application/nostr+json' https://relay.silkepilon.dev | python3 -m json.tool
```

Expected: a JSON document with `name`, `pubkey`, `supported_nips`. Confirm `pubkey` matches the relay's own identity, and that the document is served rather than the web UI's HTML.

- [ ] **Step 4: Verify the WebSocket upgrade**

```bash
curl -sS -i -N \
  -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: $(openssl rand -base64 16)" \
  https://relay.silkepilon.dev | head -5
```

Expected: `HTTP/1.1 101 Switching Protocols` (or `HTTP/2 101`). A `200` means something answered with a normal page instead of upgrading — check that Cloudflare's tunnel route points at Traefik and not at the relay's health port.

- [ ] **Step 5: Verify Prometheus is scraping the relay**

```bash
kubectl -n monitoring port-forward svc/prometheus 19090:9090 &
sleep 3
curl -fsS 'http://127.0.0.1:19090/api/v1/query?query=up{namespace="buzz"}' | python3 -m json.tool
kill %1
```

Expected: a result with `"value": [..., "1"]` for the buzz pod. An empty `result` array means the pod annotations are not matching — recheck `prometheus.io/scrape` on the relay pod template.

- [ ] **Step 6: Verify the relay accepts your owner identity**

```bash
/tmp/nak req -l 1 wss://relay.silkepilon.dev
```

Expected: either events, or an auth challenge — both prove the WebSocket handshake and the Nostr protocol layer work. A connection refused or TLS error does not.

Nothing to commit in this task.

---

### Task 9: Documentation

**Files:**
- Modify: `README.md` — the Apps table and the Secrets table.

**Interfaces:**
- Consumes: everything above.
- Produces: none.

- [ ] **Step 1: Add the Apps table row**

In `README.md`, the Apps table is sorted alphabetically. `buzz` goes between `arr-stack` and `cloudflared`. Add this line directly after the `arr-stack` row:

```markdown
| [buzz](apps/buzz) | `buzz` | [Buzz](https://github.com/block/buzz) Nostr relay — a workspace shared by humans and AI agents, with its own Postgres, Redis and MinIO |
```

- [ ] **Step 2: Add the Secrets table row**

In the Secrets table, add this line directly after the `smb-creds` row (the table is grouped loosely by app; `buzz-secret` belongs with the other app secrets):

```markdown
| `buzz-secret` | `buzz` | Relay identity + git HMAC, Postgres/Redis URLs and passwords, MinIO credentials |
```

- [ ] **Step 3: Verify both rows render**

```bash
grep -n 'apps/buzz\|buzz-secret' README.md
```

Expected: two lines, one from each table.

- [ ] **Step 4: Commit and push**

```bash
git add README.md
git commit -m "docs: add buzz to the apps and secrets tables"
git push
```

---

## Rollback

If the stack needs to come down:

```bash
git rm bootstrap/argocd/applications/buzz.yaml
git commit -m "chore(buzz): remove argo cd application"
git push
```

The Application's `prune: true` deletes the workloads. PVCs bound by Longhorn are deleted too (`reclaimPolicy: Delete` on the `longhorn` StorageClass) — **that destroys the event store.** Snapshot the volumes in the Longhorn UI first if the data matters.

`buzz-secret` and the `buzz` namespace are not managed by Argo CD's prune in the same pass; remove them by hand with `kubectl delete namespace buzz` once you are certain.
