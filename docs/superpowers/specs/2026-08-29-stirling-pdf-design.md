# Stirling PDF — design

Deploy [Stirling PDF](https://www.stirlingpdf.com) to the cluster and serve it
at `https://pdf.silkepilon.dev` through the existing Cloudflare Tunnel.

## Shape

Plain Kustomize base at `apps/stirling-pdf/`, like `n8n`: one container, one
PVC, one Service, one Ingress. Argo CD Application at
`bootstrap/argocd/applications/stirling-pdf.yaml` (Git-sourced, same as every
non-Helm app).

- Image: `stirlingtools/stirling-pdf:2.14.3` (standard flavor, amd64+arm64, so
  it can land on any node). Not `-fat` (extra Calibre/ML models) and not
  `-ultra-lite` (no LibreOffice/OCR).
- Namespace `stirling-pdf`, port 8080.

## Runtime facts (from the upstream Dockerfile / init scripts)

- Container starts as root, `init.sh` fixes ownership of the state directories
  and drops to `stirlingpdfuser` (PUID/PGID default 1000). So: no
  `runAsNonRoot`, `fsGroup: 1000`.
- State directories are symlinks under `/app` to `/configs`, `/customFiles`,
  `/pipeline`, `/logs`, `/storage`. Login users, API keys and `settings.yml`
  live in `/configs` (embedded H2 DB) — that is the data worth keeping.
- Temp files go to `/tmp/stirling-pdf` and can get large on big conversions.
- Health: `GET /api/v1/info/status` returns `{"status":"UP"}`.

## Storage

One 2Gi Longhorn PVC `stirling-pdf-data`, mounted with `subPath` at
`/configs`, `/customFiles`, `/pipeline` and `/storage`. `/logs` and `/tmp` are
`emptyDir` (`/tmp` capped at 4Gi so a runaway job can't fill a node disk).
`strategy: Recreate` because the PVC is RWO.

## Networking

Cloudflare Tunnel → Traefik `Ingress` (`web` entrypoint) → Service `stirling-pdf:80`
→ pod `:8080`. One public hostname must be added by hand in Zero Trust:
`pdf.silkepilon.dev` → `http://traefik.kube-system.svc.cluster.local:80`.
`SYSTEM_MAXFILESIZE=100` (MB) matches Cloudflare's free-plan upload cap.

## Auth

The hostname is on the public internet, so login is on
(`SECURITY_ENABLELOGIN=true`). The first admin account is seeded from a
hand-made Secret `stirling-pdf-secret` (`SECURITY_INITIALLOGIN_USERNAME`,
`SECURITY_INITIALLOGIN_PASSWORD`); after first start the user store is the H2
DB in `/configs` and the Secret is only read again on a fresh volume.
Public sign-up stays off.

## Resources

JVM + LibreOffice: request `512Mi` / limit `3Gi`, cpu request `250m` / limit
`4`. Readiness starts after 30s (JVM boot is slow on the Pis);
liveness after 120s.

## Out of scope

- OCR language packs beyond the bundled English (mount extra tessdata later).
- Pipeline automation folders, custom branding.
