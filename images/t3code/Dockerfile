# ─────────────────────────────────────────────────────────────────────────────
# ghcr.io/silkepilon/t3code — a headless T3 Code server with Claude Code and a
# general dev toolchain, so agents can clone, build, test and open PRs.
#
# Built by .github/workflows/t3code-image.yaml on every push that touches this
# directory. Deployed by apps/t3code/.
#
# $HOME (/home/node) is a Longhorn PVC at runtime, which hides anything the
# image puts there. So every tool is installed system-wide, and only state
# (logins, repos, T3 userdata) lives in $HOME.
# ─────────────────────────────────────────────────────────────────────────────
FROM node:24-bookworm

# renovate: datasource=npm depName=t3
ARG T3_VERSION=0.0.42
# renovate: datasource=npm depName=@anthropic-ai/claude-code
ARG CLAUDE_CODE_VERSION=2.1.281
# renovate: datasource=npm depName=bun
ARG BUN_VERSION=1.4.2

# gh comes from GitHub's own apt repo; Debian's package lags by years.
RUN set -eux; \
    install -d -m 0755 /etc/apt/keyrings; \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      build-essential gh git jq less openssh-client python3 python3-pip \
      python3-venv ripgrep tini unzip vim-tiny zip; \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g \
      "t3@${T3_VERSION}" \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
      "bun@${BUN_VERSION}" \
    && npm cache clean --force

# Claude Code cannot update itself here: it is installed root-owned under
# /usr/local and the container runs as node. Bumps come from the ARG above.
ENV DISABLE_AUTOUPDATER=1 \
    # Tools an agent installs with `npm i -g` / `pip install --user` land on
    # the PVC and survive restarts.
    NPM_CONFIG_PREFIX=/home/node/.local \
    PATH=/home/node/.local/bin:$PATH

USER node
WORKDIR /home/node

EXPOSE 3773

# tini reaps the agent, shell and build processes T3 spawns and forwards
# SIGTERM so the server shuts down cleanly on pod stop.
ENTRYPOINT ["/usr/bin/tini", "--"]
# ~/code is the default cwd for provider sessions; on a fresh PVC it does not
# exist yet.
CMD ["sh", "-c", "mkdir -p /home/node/code && exec t3 serve --host 0.0.0.0 --port 3773 --base-dir /home/node/.t3 /home/node/code"]
