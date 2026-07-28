# Open WebUI — Design

Date: 2026-07-28
Status: Approved

## Goal

Run Open WebUI in the homelab cluster, reachable at `https://webui.silkepilon.dev`, managed by
Argo CD like every other app in this repo.

## Decisions

| Question | Decision |
| --- | --- |
| Model backend | Configured in the admin UI, not in the manifests. Gemini via its OpenAI-compatible endpoint. No Ollama in cluster. |
| Signup | `ENABLE_SIGNUP=false`. Flipped on manually once to create the admin account. |
| Storage | 5Gi PVC, `local-path`, ReadWriteOnce. |
| Packaging | Plain Kustomize base, matching `apps/n8n`. Not Helm. |
| Image tag | Pinned to `v0.11.0` (latest release, 2026-07-27) rather than `:latest`. |

Helm was rejected because every other app here is a plain Kustomize base; a chart would be the
only exception and its values would drift from the upstream image tag. Ollama was rejected
because no node in the cluster has a GPU budget for it, and hosted models cover the use case.

## Layout

```
apps/open-webui/
  namespace.yaml
  pvc.yaml
  service.yaml
  deployment.yaml
  ingress.yaml
  kustomization.yaml
bootstrap/argocd/applications/open-webui.yaml
```

## Workload

- Image `ghcr.io/open-webui/open-webui:v0.11.0`, container port 8080.
- One replica, `strategy: Recreate` — the PVC is ReadWriteOnce, so two pods cannot both write.
- Probes all hit `/health`. A `startupProbe` (10s period, 60 failures = 10 minutes of grace)
  gates readiness and liveness. First boot runs the whole Alembic migration chain and downloads
  the embedding model from HuggingFace, and uvicorn refuses connections until that completes —
  measured at roughly 45 seconds. Without the startupProbe, liveness would begin at 60s and
  could kill the pod mid-download, restarting the download from scratch each time. Readiness
  polls every 15s, liveness every 30s, both only after startup succeeds.
- `runAsUser: 0`, `fsGroup: 0`. The upstream image ships as root and its `start.sh` writes to
  `/app/backend/data` and to the chroma cache under `$HOME`. Running non-root needs a rebuild
  with different UID/GID build args, which is out of scope.
- PVC `open-webui-data` mounted at `/app/backend/data` — holds the SQLite database (users,
  chats), uploaded RAG documents, and the cached embedding model.
- Resources: requests 500m CPU / 2Gi memory, limits 2 CPU / 4Gi memory. The bundled
  `sentence-transformers/all-MiniLM-L6-v2` embedding model loads into memory the first time RAG
  is used, so the 1Gi request used by other apps is too tight.

## Configuration

| Env var | Value | Source |
| --- | --- | --- |
| `WEBUI_URL` | `https://webui.silkepilon.dev` | inline |
| `WEBUI_SECRET_KEY` | random 32 bytes | secret `open-webui-secret` |
| `ENABLE_SIGNUP` | `"false"` | inline |
| `ENABLE_OLLAMA_API` | `"false"` | inline |
| `TZ` | `Etc/UTC` | inline |

`WEBUI_SECRET_KEY` is mandatory. Without it the app generates a fresh key on every container
start, which invalidates every existing session and logs everyone out on each restart.

`ENABLE_OLLAMA_API=false` stops the backend from repeatedly probing a non-existent Ollama at
`/ollama` and filling the logs with connection errors.

The secret is created out of band and never committed, the same convention as `n8n-secret` and
`hermes-secret`:

```sh
kubectl create secret generic open-webui-secret -n open-webui \
  --from-literal=WEBUI_SECRET_KEY="$(openssl rand -hex 32)"
```

## Model providers

No provider credentials live in the manifests. Connections are added in the running app —
Admin Panel → Settings → Connections — and stored in the database on the PVC, so they survive
restarts and Argo CD syncs.

For Gemini, add an OpenAI-compatible connection:

- URL: `https://generativelanguage.googleapis.com/v1beta/openai`
- Key: a Google AI Studio API key

`OPENAI_API_BASE_URL` / `OPENAI_API_KEY` are deliberately absent. Those env vars seed the same
settings on first boot and then override the UI, which makes the visible configuration
untrustworthy. One source of truth is better here, and the UI is the one the user actually
touches. The trade-off accepted: provider config is not in git, and is restored from the PVC
rather than from a sync.

## Networking

Cloudflare Tunnel → Traefik → Service, identical to n8n. The Ingress uses
`ingressClassName: traefik` and the `web` entrypoint; TLS terminates at Cloudflare's edge.

One manual step lives outside this repo — in Cloudflare Zero Trust → Tunnel → Public Hostnames:

- Subdomain `webui`, domain `silkepilon.dev`
- Service HTTP → `traefik.kube-system.svc.cluster.local:80`

## Bootstrapping the admin account

`ENABLE_SIGNUP=false` blocks the very first registration too, so:

1. Let Argo CD sync the app with signup disabled.
2. `kubectl set env deploy/open-webui ENABLE_SIGNUP=true -n open-webui`
3. Register at `https://webui.silkepilon.dev`. The first account created automatically becomes
   the admin.
4. Argo CD's `selfHeal: true` reverts the env var on its own; force it sooner with
   `argocd app sync open-webui` if wanted.

## Upgrades

Bump the tag in `deployment.yaml` and commit. Argo CD rolls it out. Because the tag is pinned,
a restart never silently pulls a new major version on top of an existing database.

## Out of scope

- Ollama or any in-cluster inference.
- External Postgres. SQLite on the PVC is correct for a single replica; Postgres only becomes
  necessary above one replica.
- Cloudflare Access in front of the hostname. Open WebUI's own auth with signup disabled is the
  agreed boundary.
