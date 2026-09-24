# T3 Code on the cluster — design

A headless T3 Code server (`t3 serve`) so Claude Code agents keep running
while the desktop is off. Clients connect over the tailnet only.

## Decisions

- **One instance, not three.** One `t3 serve` runs any number of projects and
  threads in parallel. Separate instances would only buy isolation between
  accounts or filesystems, which is not needed.
- **Claude Code only, subscription login.** No API key Secret. The login is
  made once with `kubectl exec` and persists on the PVC.
- **Agents may push and open PRs.** `gh` is logged in once on the PVC and set
  up as git's credential helper. No kubectl access: the pod gets no
  service-account token.
- **Custom image, built in this repo.** `images/t3code/Dockerfile` on
  `node:24-bookworm`, with `t3`, Claude Code, `gh`, bun, python3 and
  build-essential installed system-wide. `$HOME` is the PVC, so nothing in the
  image may live there. Versions are pinned `ARG`s that Renovate bumps through
  a regex manager. Installing tools at boot onto the PVC was rejected: slow
  starts that break whenever an upstream install script changes.
- **Tailscale Ingress, not the `expose` annotation.** The Ingress terminates
  HTTPS with a real cert for `t3.<tailnet>.ts.net`; the web client needs a
  secure context and the desktop app speaks wss. Never on the Cloudflare
  tunnel: reaching this server means running shell commands with the agent's
  logins.
- **Longhorn PVC as `$HOME`, 50Gi.** Repos and `node_modules` live here, and
  Longhorn lets the pod move between EliteDesks. `fsGroupChangePolicy:
  OnRootMismatch` avoids a recursive chown of `node_modules` on every start.
- **amd64 only, prefer the 16-core i7.** Builds want the cores; Pelican leans
  on the other two minis.

## Accepted limits

- A pod restart kills running agent turns (threads survive). Liveness is
  lenient, and Renovate never automerges the image digest.
- A client without Tailscale gets nothing.
