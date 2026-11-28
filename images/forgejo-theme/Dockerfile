# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# ghcr.io/silkepilon/forgejo-theme — the GitHub theme for Forgejo
# (https://github.com/whereareiam/forgejo-github-theme), built from source
# with patches/ applied on top. Data only: /theme holds the files Forgejo
# serves from its custom/ directory:
#
#   /theme/templates/          GitHub-style page layouts (bound to one Forgejo release)
#   /theme/public/assets/css/  theme-github-*.css plus the layouts' page CSS
#   /theme/public/assets/js/   the layouts' scripts
#   /theme/FORGEJO_VERSION     the Forgejo release the layouts were made for
#
# The `theme` init containers in apps/tools/forgejo/deployment.yaml copy it
# onto the volume, and install templates/ only if FORGEJO_VERSION matches the
# running Forgejo.
#
# Built by .github/workflows/forgejo-theme-image.yaml on every push that
# touches this directory.
# ─────────────────────────────────────────────────────────────────────────────
FROM oven/bun:1.4.5 AS build

# A theme release supports exactly one Forgejo release (package.json
# "forgejo"). Bump it together with the Forgejo image, and re-check
# patches/ against the new source.
# renovate: datasource=github-tags depName=whereareiam/forgejo-github-theme
ARG THEME_VERSION=1.6.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends patch \
 && rm -rf /var/lib/apt/lists/*

ADD https://github.com/whereareiam/forgejo-github-theme.git#${THEME_VERSION} /src
COPY patches/ /patches/
WORKDIR /src

# -F0: a hunk that no longer matches exactly fails the build.
RUN set -eu; for p in /patches/*.patch; do patch -p1 -F0 --forward --no-backup-if-mismatch < "$p"; done

RUN bun install --frozen-lockfile
# THEME_VERSION ends up in the footer credit.
RUN THEME_VERSION="${THEME_VERSION}+homelab" bun run bundle \
 && bun -e 'console.log(require("./package.json").forgejo)' > dist/forgejo/FORGEJO_VERSION \
 && cp LICENSE NOTICE dist/forgejo/

FROM busybox:1.38
COPY --from=build /src/dist/forgejo /theme
