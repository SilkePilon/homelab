# Forgejo

[Forgejo](https://forgejo.org) at <https://git.silkepilon.dev>, with
[Forgejo Actions](https://forgejo.org/docs/latest/user/actions/) for CI. Git
over **HTTPS only**; there is no SSH.

| Piece | Where | What |
| --- | --- | --- |
| `forgejo` | this directory, namespace `forgejo` | Rootless `codeberg.org/forgejo/forgejo` image, SQLite, one Longhorn volume, amd64 nodes |
| `forgejo-runner` | [`apps/tools/forgejo-runner`](../forgejo-runner), namespace `forgejo-runner` | `forgejo-runner` + a privileged Docker-in-Docker sidecar. Each job is a container in dind |

The forgejo pod has a second container, `setup`. It creates the admin user if
it is missing and registers the runner, then idles. You never click through an
installer or copy a runner token.

## Setup

### 1. Secrets

`forgejo-secret` is synced from the Infisical folder `/forgejo` (see
[`apps/infra/external-secrets/README.md`](../../infra/external-secrets/README.md)).
On a fresh install, add:

| Key | Generate with | Notes |
| --- | --- | --- |
| `SECRET_KEY` | `openssl rand -hex 32` | Encrypts 2FA seeds and similar in the DB. **Never change it** once set |
| `ADMIN_PASSWORD` | `openssl rand -base64 24` | Read only when the admin user does not exist yet |
| `RUNNER_SECRET` | `openssl rand -hex 20` | Exactly 40 hex chars. Shared with the runner |

The admin user is `silkepilon` (see `ADMIN_USERNAME` in
[deployment.yaml](deployment.yaml)). Its email is a placeholder; set a real one
under **Settings → Account**.

### 2. Cloudflare Tunnel

Zero Trust → Tunnel → Public Hostnames: `git.silkepilon.dev` →
`HTTP http://traefik.kube-system.svc.cluster.local:80`.

### 3. Check

```bash
kubectl -n forgejo logs deploy/forgejo -c setup   # "setup done"
kubectl -n forgejo-runner logs deploy/forgejo-runner -c runner   # "declared successfully"
```

The runner shows as **Idle** under **Site administration → Actions → Runners**.

## Using it

- Clone and push with an access token (**Settings → Applications**, scope
  `write:repository`) as the password, or log in through a credential helper.
- Cloudflare limits a request body to **100 MB**. A single push, LFS object or
  container image layer bigger than that fails. Push big histories in chunks.
- Signups are off. Public repos can be browsed without logging in.

### CI

Workflows go in `.forgejo/workflows/*.yml` (`.github/workflows` works too).
The syntax is GitHub Actions'. Runner labels (`runs-on:`):

| Label | Image |
| --- | --- |
| `docker` | `data.forgejo.org/oci/node:24-trixie`, small, no `sudo` |
| `ubuntu-latest`, `ubuntu-24.04` | `ghcr.io/catthehacker/ubuntu:act-24.04`, close to GitHub's runner, ~1 GB on first pull |

```yaml
on: [push]
jobs:
  test:
    runs-on: docker
    steps:
      - uses: actions/checkout@v4
      - run: npm ci && npm test
```

- `uses: actions/foo@v4` resolves against <https://data.forgejo.org>, which
  mirrors the common actions. For anything else use a full URL:
  `uses: https://github.com/owner/action@v1`.
- `actions/cache` works (the runner runs the cache server).
- `docker build` / `docker run` work in jobs: the dind socket is mounted at
  `/var/run/docker.sock`. Images built in one job are gone after a runner
  restart.
- Jobs run on the amd64 EliteDesks, two at a time (`capacity` in
  [`config.yml`](../forgejo-runner/config.yml)), with up to 4 CPUs and 8 GiB
  between them (the dind container's limits).

**Trust model.** A job can reach the privileged dind daemon, which means root
on the node. That is fine while signups are off and only your own repos run
CI. Do not enable Actions on repos that take pull requests from strangers.

## Theme

[GitHub theme for Forgejo](https://github.com/whereareiam/forgejo-github-theme)
(Apache-2.0): GitHub's colours and also its page layouts, such as the repo
home, PRs, diffs, Actions and settings. The default is `github-dark`; every
variant (light, auto, soft dark, high contrast, colour blind, pink,
catppuccin) can be picked under **Settings → Appearance**. Forgejo's own
themes are not offered, because the GitHub layouts are only styled by the
github-* themes.

It is built from source in [`images/forgejo-theme`](../../../images/forgejo-theme)
into `ghcr.io/silkepilon/forgejo-theme`, with the patches in
[`patches/`](../../../images/forgejo-theme/patches) on top:

- `upstream-fixes.patch`: fixes from the theme's upstream,
  [lutinglt/gitea-github-theme](https://github.com/lutinglt/gitea-github-theme),
  that the Forgejo fork does not have yet. The patch header lists them.
- `smaller-logo.patch`: Forgejo's logo at 24px instead of 32px.

Two init containers install it on every start. They replace
`custom/templates` and `custom/public/assets` on the volume, so do not keep
anything else there:

1. `theme-files` copies the image's files out.
2. `theme` runs in the Forgejo image and compares the running release with
   the one the layouts were made for (`FORGEJO_VERSION` in the image). The
   layouts are installed only when the two match. Otherwise the log says
   `CSS only, layouts skipped`, and Forgejo runs with stock pages and GitHub
   colours. A mismatched template could break pages or stop Forgejo from
   starting.

To change the theme, edit or add a patch and push. The workflow rebuilds the
image and Renovate bumps the digest in [deployment.yaml](deployment.yaml).
A patch that no longer applies fails the build.

## Upgrades

Renovate opens PRs for Forgejo and the theme but does not merge them: a
theme release supports exactly one Forgejo release. To upgrade:

1. Wait for a theme release for the new Forgejo version. Its release notes
   name the version.
2. Bump `THEME_VERSION` in
   [`images/forgejo-theme/Dockerfile`](../../../images/forgejo-theme/Dockerfile).
   Drop the hunks of `upstream-fixes.patch` the fork has picked up, and
   rebuild.
3. Bump the Forgejo tag in all three containers of
   [deployment.yaml](deployment.yaml) (`theme`, `forgejo`, `setup`), along
   with the new image digest.

Forgejo majors come every ~3 months, run DB migrations and cannot be rolled
back: read the [release notes](https://codeberg.org/forgejo/forgejo/releases)
and snapshot the `forgejo-data` Longhorn volume first. The runner image still
updates on its own.

## Backups

Everything is on the `forgejo-data` volume: repositories, the SQLite DB,
`custom/conf/app.ini`. Longhorn snapshots cover it. A portable dump:

```bash
kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo dump --type tar.zst -f /tmp/gitea/dump.tar.zst
kubectl -n forgejo cp -c forgejo $(kubectl -n forgejo get pod -l app.kubernetes.io/name=forgejo -o name | cut -d/ -f2):/tmp/gitea/dump.tar.zst ./forgejo-dump.tar.zst
```

A restore needs the same `SECRET_KEY` from Infisical.
