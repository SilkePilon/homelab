<h1 align="center">
  <br>
  <a href="/"><img src=".github/media/homelab-header.png" alt="homelab" width="900"></a>
  <br>
</h1>

<p align="center">
  <img alt="k3s" src="https://img.shields.io/badge/k3s-cluster-FFC61C?logo=k3s&logoColor=black">
  <img alt="Argo CD" src="https://img.shields.io/badge/Argo%20CD-GitOps-EF7B4D?logo=argo&logoColor=white">
  <img alt="Kustomize" src="https://img.shields.io/badge/config-Kustomize-326CE5?logo=kubernetes&logoColor=white">
  <img alt="Infisical" src="https://img.shields.io/badge/secrets-Infisical-F5D90A">
  <img alt="Last commit" src="https://img.shields.io/github/last-commit/SilkePilon/homelab">
</p>

GitOps repo for my homelab: a 7-node [k3s](https://k3s.io) cluster (3 HP
minis, 4 Raspberry Pi 5s). [Argo CD](https://argo-cd.readthedocs.io) keeps the
cluster equal to `main`. **Change a YAML file, push, done.** Nothing is applied
by hand.

## How it works

```mermaid
flowchart LR
  dev([git push]) --> repo[(this repo)]
  repo --> root[root app]
  root --> appset[ApplicationSet<br/>one app per folder]
  root --> helm[Helm apps<br/>longhorn, plane, ...]
  appset --> k3s{{k3s cluster}}
  helm --> k3s
  infisical[(Infisical)] -- External Secrets --> k3s
```

- `bootstrap/argocd/root.yaml` is applied once. It watches
  `bootstrap/argocd/applications/`.
- The **ApplicationSet** there turns every folder `apps/<group>/<app>/` into an
  Argo CD app named `<app>`, in namespace `<app>`.
- **Helm apps** have their own Application file in the same folder, because
  their chart values live there.
- **Secrets** live in Infisical, not in git. See [Secrets](#secrets).

## Traffic

```mermaid
flowchart LR
  internet([Internet]) --> cf[Cloudflare] --> tunnel[cloudflared] --> traefik[Traefik] --> sites[public apps]
  tailnet([Tailnet]) --> ts[Tailscale operator] --> admin[admin UIs<br/>Longhorn, Pi-hole, t3code]
  lan([LAN]) --> dns[Pi-hole :53<br/>on every node]
```

No router ports are open. `cloudflared` dials out to Cloudflare. Admin UIs
are reachable only over Tailscale. Storage is [Longhorn](apps/infra/longhorn)
(3 replicas); the media library is an SMB share on the NAS.

## Apps

| Group | Apps |
| --- | --- |
| **infra** | [cloudflared](apps/infra/cloudflared) · [external-secrets](apps/infra/external-secrets) · [kured](bootstrap/argocd/applications/kured.yaml) · [longhorn](apps/infra/longhorn) · [monitoring](apps/infra/monitoring) · [node-maintenance](apps/infra/node-maintenance) · [pihole](apps/infra/pihole) · [tailscale](apps/infra/tailscale) |
| **media** | [arr-stack](apps/media/arr-stack): qBittorrent, Sonarr, Radarr, Lidarr behind a ProtonVPN sidecar |
| **home** | [homeassistant](apps/home/homeassistant) |
| **tools** | [gitlab](apps/tools/gitlab) + [gitlab-runner](apps/tools/gitlab-runner) · [n8n](apps/tools/n8n) · [plane](apps/tools/plane) · [stirling-pdf](apps/tools/stirling-pdf) · [t3code](apps/tools/t3code) · [twenty](apps/tools/twenty) |
| **sites** | [docs](apps/sites/docs) · [groepshike](apps/sites/groepshike) · [jotihunt](apps/sites/jotihunt) · [portfolio](apps/sites/portfolio) · [windesheim-readers](apps/sites/windesheim-readers) |

Apps with more moving parts have a `README.md` in their folder.

## Layout

```text
apps/<group>/<app>/        Kustomize base per app (+ README.md, externalsecret.yaml)
apps/_template/            starting point for a new app
bootstrap/argocd/          root app, the ApplicationSet, and the Helm apps
docs/                      cluster notes: nodes, storage, HA, recoveries
images/                    custom images, built by .github/workflows/
scripts/                   one-off helpers (secret migration, restores)
```

## Add an app

1. `cp -r apps/_template apps/<group>/<app>` and fill in the manifests.
   The folder name is the app name and namespace, and must be unique.
2. Secrets: add a folder in Infisical and an `externalsecret.yaml`
   ([how](apps/infra/external-secrets/README.md#using-it-in-an-app)).
3. Push. The ApplicationSet picks it up.

Removing a folder does **not** delete the app (safety). Delete it in Argo CD
once you mean it.

## Secrets

```mermaid
flowchart LR
  ui[Infisical dashboard] --> inf[(Infisical<br/>homelab / prod)]
  inf -- hourly --> eso[External Secrets Operator] --> sec[Kubernetes Secret] --> pod[pod]
```

- Edit values in the [Infisical dashboard](https://eu.infisical.com), never
  with `kubectl`.
- One folder per Secret. The full list is in
  [apps/infra/external-secrets](apps/infra/external-secrets/README.md#all-secrets).
- The only secret made by hand is ESO's own Infisical login,
  `infisical-universal-auth`. Keep a copy in a password manager.

## Bootstrap a new cluster

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl create namespace external-secrets
kubectl -n external-secrets create secret generic infisical-universal-auth \
  --from-literal=clientId='...' --from-literal=clientSecret='...'
kubectl apply -f bootstrap/argocd/root.yaml
```

Node prep (Longhorn, SMB) is in [docs/](docs).

## Docs

- [High availability](docs/high-availability.md) · [Raspberry Pi 5 nodes](docs/raspberry-pi-5-nodes.md) · [Node storage](docs/node-storage.md)
- [Longhorn prerequisites](docs/longhorn-prerequisites.md) · [Monitoring migration](docs/monitoring-migration.md)
- Incidents: [pi52 recovery](docs/pi52-recovery.md) · [Pi decommission](docs/pi-decommission.md)

<sub>Header image: `.github/media/generate-header.sh`, adapted from
[resend/n8n-nodes-resend](https://github.com/resend/n8n-nodes-resend/tree/main/.github/media).</sub>
