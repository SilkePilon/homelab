# GitLab

[GitLab](https://about.gitlab.com) Community Edition, reachable at
<https://gitlab.silkepilon.dev>. Git over **HTTPS only** — there is no SSH.

Design notes: [docs/superpowers/specs/2026-10-22-gitlab-design.md](../../docs/superpowers/specs/2026-10-22-gitlab-design.md).

| Piece | Where | What |
| --- | --- | --- |
| `gitlab` | this directory, namespace `gitlab` | Omnibus `gitlab/gitlab-ce` image: nginx, Puma, Sidekiq, Gitaly, bundled Postgres + Redis in one pod, amd64 only |
| `gitlab-runner` | [`bootstrap/argocd/applications/gitlab-runner.yaml`](../../bootstrap/argocd/applications/gitlab-runner.yaml), namespace `gitlab-runner` | Upstream Helm chart, Kubernetes executor — each CI job is a pod |

Off on purpose: container registry, Pages, KAS, SSH, the bundled Prometheus.

## Setup

### 1. Secret

```bash
kubectl create namespace gitlab
kubectl -n gitlab create secret generic gitlab-secret \
  --from-literal=GITLAB_ROOT_PASSWORD="$(openssl rand -base64 24)"
```

Read on first boot only (empty `/var/opt/gitlab`). Afterwards change the root
password in the UI; editing the Secret does nothing.

### 2. Cloudflare Tunnel

Zero Trust → Tunnel → Public Hostnames: `gitlab.silkepilon.dev` →
`HTTP http://traefik.kube-system.svc.cluster.local:80`.

### 3. First boot

The first start runs every migration and takes 5-10 minutes; the startup probe
allows ~11. Follow it with:

```bash
kubectl -n gitlab logs -f deploy/gitlab
```

### 4. Lock it down

The instance is on the public internet. Log in as `root`, then under
**Admin → Settings → General**:

- **Sign-up restrictions** → untick *Sign-up enabled*.
- **Visibility and access controls** → *Enabled Git access protocols* →
  **Only HTTP(S)**, so no SSH clone URLs are shown.

Enable 2FA on the root account. Create your own user and make it admin rather
than working as `root`.

Without SMTP there are no notification or password-reset emails.

### 5. Register the runner

1. **Admin → CI/CD → Runners → New instance runner**. Tick *Run untagged
   jobs* (or set tags), create it, and copy the `glrt-…` token.
2. Store it (never committed):

   ```bash
   kubectl create namespace gitlab-runner
   kubectl -n gitlab-runner create secret generic gitlab-runner-secret \
     --from-literal=runner-token='glrt-...' \
     --from-literal=runner-registration-token=''
   ```

3. Restart the runner if it was already crash-looping:
   `kubectl -n gitlab-runner rollout restart deploy/gitlab-runner`.
   The runner shows as online in the Runners list within a minute.

## Using it

- Clone and push with a personal access token (scope `read_repository` /
  `write_repository`) as the password, or let a credential helper store it.
- Cloudflare limits a request body to **100 MB**, so a single push or LFS
  object bigger than that fails. Push big histories in chunks.
- CI jobs run on the amd64 nodes by default, unprivileged. Build images with
  kaniko or buildah, not docker-in-docker. To run a job on a Pi:

  ```yaml
  variables:
    KUBERNETES_NODE_SELECTOR_ARCH: "kubernetes.io/arch=arm64"
  ```

## Upgrades

Bump `image:` in [deployment.yaml](deployment.yaml) along the
[required upgrade path](https://docs.gitlab.com/update/upgrade_paths/) — one
required stop per commit. Before the next bump, wait until **Admin →
Monitoring → Background migrations** shows nothing pending. Each bump is a few
minutes of downtime (`Recreate`). Keep the runner chart's `targetRevision`
roughly in step.

## Backups

Not automated. By hand:

```bash
kubectl -n gitlab exec deploy/gitlab -- gitlab-backup create
kubectl -n gitlab exec deploy/gitlab -- ls /var/opt/gitlab/backups
kubectl -n gitlab cp gitlab/$(kubectl -n gitlab get pod -l app.kubernetes.io/name=gitlab -o name | cut -d/ -f2):/var/opt/gitlab/backups/<file>_gitlab_backup.tar ./
kubectl -n gitlab exec deploy/gitlab -- cat /etc/gitlab/gitlab-secrets.json > gitlab-secrets.json
```

The backup tar does **not** include `gitlab-secrets.json`. Without it a
restore cannot decrypt CI variables, 2FA or tokens. Keep both, somewhere safe.
