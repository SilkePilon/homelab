# T3 Code

A headless [T3 Code](https://github.com/pingdotgg/t3code) server (`t3 serve`)
with Claude Code, so agents keep running while the desktop is off. Any T3
client on the tailnet connects to `https://t3.<tailnet>.ts.net`.

- **Image:** `ghcr.io/silkepilon/t3code`, built from
  [`images/t3code/`](../../images/t3code/Dockerfile) by
  [`.github/workflows/t3code-image.yaml`](../../.github/workflows/t3code-image.yaml).
  It holds Claude Code, `gh`, git, node 24, bun, python3, build-essential and
  ripgrep, pinned as `ARG`s and bumped by Renovate.
- **T3 itself:** follows the **nightly** channel, independent of the image.
  See [Updates](#updates).
- **State:** the `t3code-home` Longhorn PVC is mounted as `$HOME`
  (`/home/node`). It holds T3's threads and paired sessions, the Claude and
  `gh` logins, git config, and cloned repos in `~/code`.
- **Access:** a Tailscale Ingress only. It is not on the Cloudflare tunnel or
  the LAN.

## First-time setup

After Argo CD has synced and the pod is Running, do these once. Everything
lands on the PVC and survives restarts.

```bash
# Shell into the agent's environment.
kubectl -n t3code exec -it deploy/t3code -- bash

# 1. Claude Code, with the Pro/Max subscription. Run `claude`, then /login,
#    open the printed URL on any device and paste the code back. Then /exit.
claude

# 2. GitHub: push access and PRs for gh and git.
gh auth login          # GitHub.com → HTTPS → paste a token or use the device code
gh auth setup-git

# 3. Commit identity.
git config --global user.name  "SilkePilon"
git config --global user.email "..."

# 4. Clone the repos the agents should work on.
cd ~/code && gh repo clone SilkePilon/homelab
```

## Pairing a client

Every new client (desktop app, browser, phone) needs a one-time token:

```bash
kubectl -n t3code exec -it deploy/t3code -- \
  t3 pair --base-dir /home/node/.t3 --label laptop --ttl 10m
```

The command prints a token, a pairing URL and a QR code. The URL carries the
pod IP, which is not reachable from outside the cluster: swap
`http://<pod-ip>:3773` for `https://t3.<tailnet>.ts.net` and open it, or add
the server in the desktop app with that host and the token. Revoke clients
with `t3 auth session` (same `exec` and `--base-dir`).

## Security

Anyone who can reach this server and holds a paired session can run shell
commands with the agent's GitHub and Claude logins. Two layers keep that
closed: the tailnet ACL (only devices allowed to reach `tag:k8s`) and T3's
own pairing. The pod has no Kubernetes service-account token, runs as uid
1000 and drops all capabilities.

## Updates

**T3** updates itself. The image runs
[`t3-supervisor`](../../images/t3code/t3-supervisor.sh) instead of `t3 serve`
directly. On boot and then every 2 hours it asks npm for the newest version on
`T3_CHANNEL` (`nightly`), downloads it with `t3 update` into
`~/.t3/runtime/versions/` on the PVC, and restarts the server in place. The
pod does not roll. The last 3 versions are kept. If the registry is down, the
newest version already on the PVC keeps running.

A restart kills every agent turn running at that moment, so a new nightly can
cut one off mid-turn. Threads survive; resend the message. To follow a
different channel or interval, change `T3_CHANNEL` / `T3_UPDATE_INTERVAL` in
`deployment.yaml`.

```bash
kubectl -n t3code logs deploy/t3code | grep t3-supervisor   # update history
```

**The image** (Claude Code, gh, toolchain) does not roll on its own. Renovate
opens a PR for each `ARG` bump in the Dockerfile, CI builds a new `latest` on
merge, and Renovate then opens a second PR moving the digest in
`deployment.yaml`. That second PR is never automerged, because a rollout kills
every agent turn running at that moment. Merge it when nothing is running.
