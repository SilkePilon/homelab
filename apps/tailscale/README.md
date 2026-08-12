# Tailscale

The Tailscale Kubernetes operator, so the cluster is reachable over the tailnet
without exposing anything to the internet.

Unlike every other app here there is no Kustomize base — the operator is a Helm
chart and the Argo CD Application renders it directly. The manifest lives at
[`bootstrap/argocd/applications/tailscale.yaml`](../../bootstrap/argocd/applications/tailscale.yaml).

## What it gives you

- **API server proxy** — run `kubectl` against the cluster from any device on
  the tailnet, with no port 6443 exposed and no kubeconfig copying. Enabled
  here via `apiServerProxyConfig.mode: "true"`.
- **Ingress** — put a Service or Ingress on the tailnet by annotating it. No
  new Application needed per service.
- **Egress** — let cluster workloads reach other tailnet devices.
- **Connector / ProxyGroup / Recorder CRDs** — subnet routers, exit nodes and
  session recording, if you want them later.

## Setup

The operator authenticates with an OAuth client. That credential is **never
committed** — same rule as `n8n-secret` and `grafana-admin-creds`.

### 1. ACL tags

At <https://login.tailscale.com/admin/acls>, make sure these exist:

```json
{
  "tagOwners": {
    "tag:k8s-operator": ["autogroup:admin"],
    "tag:k8s":          ["tag:k8s-operator"]
  }
}
```

`tag:k8s-operator` is what the operator itself runs as; `tag:k8s` is what it
tags the proxies it creates. The operator must own `tag:k8s` or it cannot
register them.

### 2. OAuth client

At <https://login.tailscale.com/admin/settings/trust-credentials>, create a
credential with these scopes:

- General > Services: **Read and Write**
- Devices > Core: **Read and Write**
- Keys > Auth Keys: **Read and Write**

Tag it `tag:k8s-operator`. Copy the Client ID and Client secret.

### 3. Create the secret

```bash
kubectl create namespace tailscale
kubectl create secret generic operator-oauth -n tailscale \
  --from-literal=client_id='YOUR_CLIENT_ID' \
  --from-literal=client_secret='YOUR_CLIENT_SECRET'
```

The chart's `oauth.clientId` / `oauth.clientSecret` values are left empty on
purpose. Empty means the chart creates no Secret and mounts this one instead.
**Do not** set them in the Application — that would put the secret in git.

### 4. Let Argo CD sync

The app-of-apps picks up `bootstrap/argocd/applications/tailscale.yaml`
automatically. To sync it by hand:

```bash
kubectl -n argocd annotate application tailscale \
  argocd.argoproj.io/refresh=hard --overwrite
```

Check it came up:

```bash
kubectl -n tailscale get pods
kubectl -n tailscale logs -l app.kubernetes.io/name=tailscale-operator --tail=50
```

A device named `homelab-k8s-operator` should appear in the tailnet.

## Using the API server proxy

Grant access in the tailnet policy file, then point kubectl at the tailnet
hostname:

```json
{
  "grants": [{
    "src": ["autogroup:admin"],
    "dst": ["tag:k8s-operator"],
    "app": {
      "tailscale.com/cap/kubernetes": [{
        "impersonate": { "groups": ["system:masters"] }
      }]
    }
  }]
}
```

```bash
tailscale configure kubeconfig homelab-k8s-operator
kubectl get nodes
```

Because `allowImpersonation: "true"` is set, the proxy impersonates the tailnet
identity, so cluster RBAC applies per user rather than everyone sharing the
admin cert.

## Exposing a service to the tailnet

Annotate any Service — no new Argo app required:

```yaml
metadata:
  annotations:
    tailscale.com/expose: "true"
    tailscale.com/hostname: grafana   # optional, defaults to <ns>-<svc>
```

The operator creates a proxy StatefulSet in the `tailscale` namespace and the
service becomes reachable at `grafana.<your-tailnet>.ts.net`.

## Notes

- The operator and its proxies run as regular workloads, so they schedule on
  any node. The images are multi-arch, so the arm64 Pis are fine.
- Upgrades are a `targetRevision` bump in the Application. Chart versions track
  the Tailscale client version.
- Removing a `tailscale.com/expose` annotation makes the operator tear the
  proxy down and remove the device from the tailnet.
