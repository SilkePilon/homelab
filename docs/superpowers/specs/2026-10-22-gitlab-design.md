# GitLab CE — design

Self-host [GitLab](https://about.gitlab.com) Community Edition at
`https://gitlab.silkepilon.dev` through the existing Cloudflare Tunnel, plus an
in-cluster GitLab Runner so CI pipelines actually run.

## Decisions

| Question | Answer |
| --- | --- |
| Access | Public web via Cloudflare Tunnel. Git over **HTTPS only** — no SSH anywhere. |
| Extras | CI/CD runner. No container registry, no Pages, no KAS. |
| Install shape | Omnibus `gitlab/gitlab-ce` image, one pod, Kustomize base. The cloud-native `gitlab/gitlab` chart (≈15 workloads, 8-10 GiB, needs object storage) is overkill for one user. |
| Runner | Upstream `gitlab-runner` Helm chart as its own Argo CD Application, Kubernetes executor. |

## 1. GitLab — `apps/gitlab/` (namespace `gitlab`)

- Image `gitlab/gitlab-ce:19.5.1-ce.0` (multi-arch, but pinned to
  `kubernetes.io/arch: amd64` for memory: the EliteDesks have the headroom, the
  8 GiB Pis do not).
- `Deployment`, 1 replica, `strategy: Recreate` (RWO volumes).
  Requests `cpu: 500m, memory: 3Gi`; limits `cpu: 4, memory: 6Gi`.
- Volumes:
  | Name | Kind | Mount | Notes |
  | --- | --- | --- | --- |
  | `gitlab-config` | Longhorn PVC 1Gi | `/etc/gitlab` | Holds `gitlab-secrets.json` (DB encryption keys for CI variables, 2FA, tokens). Must be backed up alongside data. |
  | `gitlab-data` | Longhorn PVC 30Gi | `/var/opt/gitlab` | Repos, bundled Postgres + Redis, uploads. Longhorn allows expansion. |
  | `logs` | `emptyDir` | `/var/log/gitlab` | Logs are not worth persisting. |
  | `shm` | `emptyDir` (Memory, 256Mi) | `/dev/shm` | Puma/Sidekiq metrics need more than the 64Mi default. |
- `GITLAB_OMNIBUS_CONFIG` (plain env, no secrets in it):
  - `external_url 'https://gitlab.silkepilon.dev'`
  - nginx: `listen_port 80`, `listen_https false`, `proxy_set_headers`
    `X-Forwarded-Proto https` / `X-Forwarded-Ssl on`, `real_ip_trusted_addresses`
    = `10.42.0.0/16` (k3s pod CIDR — Traefik connects from there), `real_ip_header X-Forwarded-For`.
  - `letsencrypt['enable'] = false`
  - Off: `registry`, `gitlab_pages`, `gitlab_kas`, `prometheus_monitoring`
    (and with it the bundled exporters), `gitlab_sshd`; `gitlab_rails['gitlab_shell_ssh_port']`
    left default but irrelevant since SSH is disabled in the UI.
  - Trim: `puma['worker_processes'] = 2`, `sidekiq['concurrency'] = 10`.
  - `gitlab_rails['monitoring_whitelist']` = `127.0.0.0/8`, `10.42.0.0/16`,
    `192.168.0.0/24` so kubelet probes can hit the health endpoints.
  - `gitlab_rails['time_zone'] = 'Europe/Amsterdam'`.
- Probes on port 80: `startupProbe` `/-/readiness` with ~10 min budget (first
  boot runs migrations), `readinessProbe` `/-/readiness`, `livenessProbe`
  `/-/liveness`.
- `Service` `gitlab` (ClusterIP, port 80 → `http`).
- `Ingress` `gitlab.silkepilon.dev`, `ingressClassName: traefik`, entrypoint
  `web`.

### Secret

`gitlab-secret` in `gitlab`, never committed:
- `GITLAB_ROOT_PASSWORD` — only read when `/var/opt/gitlab` is empty (first
  boot). Changing it later does nothing; change the password in the UI.

### Manual steps after first sync

1. Cloudflare Zero Trust: public hostname `gitlab.silkepilon.dev` →
   `http://traefik.kube-system.svc.cluster.local:80`.
2. Log in as `root`. Admin → Settings → General → Visibility and access
   controls → **Enabled Git access protocols: Only HTTP(S)**. Also turn off
   public sign-up (Admin → Settings → General → Sign-up restrictions), since the
   instance is on the public internet.

### Known limit

Cloudflare's free plan caps a request body at 100 MB, so a single HTTPS push
(or LFS object) larger than that fails. Fine for source code.

## 2. Runner — `bootstrap/argocd/applications/gitlab-runner.yaml`

- Chart `https://charts.gitlab.io` → `gitlab-runner` `0.94.0` (runner 19.5.0),
  release `gitlab-runner`, namespace `gitlab-runner` (`CreateNamespace=true`).
- `gitlabUrl: http://gitlab.gitlab.svc.cluster.local/` and runner
  `clone_url` set to the same, so registration, job polling and `git clone`
  stay inside the cluster instead of looping out through Cloudflare.
- Auth: runner created in the GitLab UI (Admin → CI/CD → Runners → New instance
  runner), its `glrt-…` token stored in `gitlab-runner-secret`
  (`runner-token` = token, `runner-registration-token` = empty) and referenced
  via `runners.secret`. Not committed.
- `concurrent: 4`, `rbac.create: true`, Kubernetes executor in namespace
  `gitlab-runner`.
- Job pods: default `node_selector` `kubernetes.io/arch: amd64` (many CI images
  are amd64-only; avoids `exec format error` on the Pis).
  `node_selector_overwrite_allowed: "kubernetes.io/arch=.*"` so a job can set
  `KUBERNETES_NODE_SELECTOR_ARCH: kubernetes.io/arch=arm64` to run on a Pi.
- Job default resources: request `500m` / `1Gi`, limit `2` / `4Gi`; helper
  `100m` / `128Mi`.
- Unprivileged. Image builds use kaniko or buildah, not docker-in-docker.
- The runner Deployment itself is tiny; no arch pin needed (multi-arch image).

## 3. Upgrades

GitLab must be upgraded along its
[required upgrade path](https://docs.gitlab.com/update/upgrade_paths/): bump the
image tag one required stop at a time and wait for background migrations to
finish (Admin → Monitoring → Background migrations) before the next bump. Each
bump is a few minutes of downtime (`Recreate`). Runner chart and GitLab minor
versions should stay roughly aligned.

## 4. Docs

- `apps/gitlab/README.md`: secret creation, Cloudflare hostname, first login +
  hardening steps, runner registration, upgrade rule, manual backup command
  (`gitlab-backup create` + copy `/etc/gitlab/gitlab-secrets.json`).
- Root `README.md`: add `gitlab` and `gitlab-runner` to the app table and
  `gitlab-secret` / `gitlab-runner-secret` to the secrets table.

## Out of scope

- SMTP (no email notifications or password reset mails until configured).
- Container registry, Pages, KAS, SSH git.
- Automated backups — same story as every other app here.
