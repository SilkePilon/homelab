# homelab

GitOps source of truth for my homelab Kubernetes (k3s) cluster.

**Argo CD** watches this repo and reconciles the cluster to match what's committed here.
Application manifests are **plain Kustomize**, natively understood by Argo CD.

## Repository layout

```text
apps/                      # Application Kustomize bases (the real K8s resources)
  hermes-agent/            # First app: Hermes Agent (Nous Research)
infrastructure/            # Cluster-wide platform config
  external-secrets/        #   ClusterSecretStore "homelab" (1Password)
bootstrap/
  argocd/                  # Argo CD entrypoints (app-of-apps)
    root.yaml              #   root Application -> watches applications/
    applications/          #   one child Application per app/infra item
      hermes-agent.yaml
      external-secrets-store.yaml
schematics/                # Hardware/rack diagrams
```

Adding an app: create `apps/<name>/` (Kustomize base), then add
`bootstrap/argocd/applications/<name>.yaml` pointing at that base.

## Prerequisites

- A running **k3s** cluster (ships Traefik + `local-path` StorageClass).
- **Argo CD** installed in the cluster.
- **External Secrets Operator (ESO)** + a `ClusterSecretStore` named `homelab`
  (for syncing API keys). Optional — see [Secrets](#secrets).

## Bootstrap

```bash
# Install Argo CD (once)
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Point Argo at this repo (app-of-apps). It then deploys everything under apps/.
kubectl apply -f bootstrap/argocd/root.yaml
```

## Apps

### Hermes Agent

Self-hosted, self-improving AI agent by [Nous Research](https://github.com/NousResearch/hermes-agent)
(MIT). Runs as a persistent **gateway** process that talks over messaging
channels (Signal, Telegram, Discord, …) and ships a web admin dashboard. There
is no public web chat UI.

What the manifests do:

- **Deployment** (`replicas: 1`, `strategy: Recreate`) — only one gateway may
  ever touch the data volume at a time.
- **PVC** `/opt/data` — single source of truth: config, `.env`, memories,
  skills, sessions. Survives restarts and image upgrades.
- **Dashboard** on port `9119` (enabled, runs in insecure/no-OAuth mode for
  trusted-LAN access). Gateway API/health on port `8642`.
- **Service** is `ClusterIP` only — reach the dashboard via `port-forward`.

#### First-time configuration

The agent is configured once, interactively, writing into the PVC:

```bash
# Wait for the pod, then run the setup wizard inside it
kubectl -n hermes-agent exec -it deploy/hermes-agent -- hermes setup
```

This collects your LLM provider key (e.g. OpenRouter) and lets you wire a
messaging channel. To use **Signal**, follow the wizard's channel setup (links a
signal-cli identity). Config is persisted to `/opt/data` on the PVC.

#### Access the dashboard locally

```bash
kubectl -n hermes-agent port-forward svc/hermes-agent 9119:9119
# open http://localhost:9119
```

> ⚠️ The dashboard runs with `HERMES_DASHBOARD_INSECURE=1`, which exposes API
> keys/session data to anyone who can reach the port. The Service is therefore
> ClusterIP-only and intended to be reached solely via `port-forward`. Don't add
> an Ingress without first enabling the OAuth gate.

#### Upgrading

Pin the image tag in [apps/hermes-agent/deployment.yaml](apps/hermes-agent/deployment.yaml)
to a released version (e.g. `:v2026.5.29.2`) and bump it via PR — GitOps then
rolls it out. The PVC data is untouched across upgrades.

## Secrets

Secrets are managed with **External Secrets Operator (ESO)** backed by
**1Password** (via the official 1Password *SDK* provider — no Connect server
needed). Each app declares an `ExternalSecret` that pulls from the cluster-wide
[`homelab` ClusterSecretStore](infrastructure/external-secrets/clustersecretstore.yaml)
and renders a Kubernetes `Secret`.

How a secret flows:

```text
1Password vault "homelab"
  └─ item "hermes-agent" / field "openrouter_api_key"
        ▲ read by
ClusterSecretStore "homelab"  (onepasswordSDK + service-account token)
        ▲ referenced by
ExternalSecret "hermes-agent-secrets"  (apps/hermes-agent)
        ▼ renders
Secret "hermes-agent-secrets"  ──envFrom──▶  Hermes Deployment
```

### One-time setup

These three steps can't live in Git (they bootstrap trust):

1. **Install ESO** (ships the CRDs the store/ExternalSecret depend on):

   ```bash
   helm repo add external-secrets https://charts.external-secrets.io
   helm install external-secrets external-secrets/external-secrets \
     -n external-secrets --create-namespace
   ```

2. **Create a 1Password Service Account** with read access to your vault, then
   store its token in the cluster:

   ```bash
   kubectl -n external-secrets create secret generic onepassword-token \
     --from-literal=token='ops_eyJ...'
   ```

   Service-account docs: <https://developer.1password.com/docs/service-accounts/>

3. **Create the items** in your 1Password `homelab` vault. For Hermes, an item
   titled `hermes-agent` with a field labeled `openrouter_api_key`.

The `ClusterSecretStore` and per-app `ExternalSecret`s are then reconciled by
Argo CD (`bootstrap/argocd/applications/external-secrets-store.yaml` and each app).

### Adding a key

1. Add a field to the 1Password item (or a new item).
2. Add an entry under `data:` in the app's `ExternalSecret`, using the SDK key
   format `key: <item>/<field>` (no `property:`).

### Notes

- The store points at **one vault** (`vault: homelab` in the store manifest) —
  change it to match your 1Password vault name.
- Until ESO + the token are in place, Hermes still runs: the Deployment
  references the secret with `optional: true`, so you can configure keys via
  `hermes setup` instead.
