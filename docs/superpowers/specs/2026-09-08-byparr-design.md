# Byparr replaces FlareSolverr — design

Swap the Cloudflare challenge solver in `apps/arr-stack` from
[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) v3.5.0 to
[Byparr](https://github.com/ThePhaseless/Byparr) 3.0.4. The pod layout stays
the same: gluetun sidecar + solver + ext-to-torznab sharing one network
namespace, so solved `cf_clearance` cookies stay bound to the VPN exit IP.

## What Byparr actually offers (from `src/endpoints.py`, `src/models.py` at v3.0.4)

- `POST /v1` only. `cmd` is accepted for compatibility and ignored: every call
  is a page load (`request.get` semantics). `session`, `postData`,
  `tabs_till_verify` are unknown fields and silently dropped.
- No `sessions.create` / `sessions.destroy`. A `sessions.create` call hits
  the default `url="https://"`, fails navigation and answers HTTP 502.
- No `request.post`. Form POSTs cannot go through the browser.
- Each request launches a fresh camoufox Firefox, solves, returns
  `{status:"ok", solution:{status:200, cookies:[...], userAgent, response}}`
  and closes the browser. `solution.status` is always 200.
- Failures are HTTP errors: 408 (timeout), 502 (target unreachable), with a
  JSON `detail` body.
- `maxTimeout` >= 1000 is treated as milliseconds (FlareSolverr style).
- `GET /health` performs a real browser fetch of `https://google.com`
  (several seconds, one Firefox). `GET /` is a 301 to `/docs`.
- Runs as uid 1000, needs a writable `/dev/shm` of a few hundred MB.
- Image: `ghcr.io/thephaseless/byparr:3.0.4` (no `v` prefix), amd64 + arm64.
  Upstream calls ARM support minimal; the pod stays pinned to amd64.

## ext-to-torznab changes (release 1.3.0)

`FlareSolverrClient` learns a backend mode, `FLARESOLVERR_BACKEND`:

| value | sessions | `request.post` | detection |
| --- | --- | --- | --- |
| `flaresolverr` | yes | yes | — |
| `byparr` | no | no | — |
| `auto` (default) | probed once, lazily | | `GET <base>/` without following redirects: a 3xx (Byparr's `/docs` redirect) or a JSON `msg` containing `Byparr` means byparr, anything else means flaresolverr. A failed probe is not cached. |

Behaviour in byparr mode:

- `get_page_with_cookies` posts `{"cmd":"request.get","url","maxTimeout"}`
  with no `session`. One retry on transport error, no session replacement,
  no idle reaper.
- `post_form` raises `FlareSolverrError` immediately (unsupported).
- `SiteClient.post_json`: when the direct POST is challenged and the solver
  cannot POST, the client re-solves `<base>/browse/` through the solver
  (which imports fresh cookies + UA and unblocks direct) and retries the
  direct POST once. This is the only way magnet lookups can survive a
  clearance expiry with Byparr.

Unchanged and still relevant: `FLARESOLVERR_URL`, `FLARESOLVERR_TIMEOUT`.
Ignored in byparr mode: `FLARESOLVERR_TABS_TILL_VERIFY`,
`FLARESOLVERR_SESSION_IDLE`, `FLARESOLVERR_SESSION_TTL`.

## Homelab manifest changes

- `apps/arr-stack/flaresolverr.yaml` becomes `byparr.yaml`; Deployment and
  Service renamed `byparr`. All consumers follow: `ext-to-torznab` Service
  selector, `gluetun-proxy.yaml` RBAC `resourceNames` and the
  `rollout restart` target, Argo CD `ignoreDifferences`, README, HA doc.
- Byparr container: `LOG_LEVEL=info`, `TZ`, port 8191, `/dev/shm` as an
  `emptyDir` with `medium: Memory`, `sizeLimit: 512Mi`. Requests
  512Mi / limits 2Gi (one Firefox per solve, plus `/health` may overlap).
- Probes: startup `GET /health` (timeout 60s, period 15s, up to 10 min, VPN
  cold start), readiness TCP 8191 (cheap, no browser launch), liveness
  `GET /health` every 10 min (also proves the tunnel still resolves and
  egresses).
- ext-to-torznab env: `FLARESOLVERR_BACKEND=byparr`, drop the three
  session/turnstile variables, keep `FLARESOLVERR_TIMEOUT=120000`. Image
  moves to the `1.3.0` tag; Renovate pins the digest afterwards.

## Answers to the operational questions

- `FLARESOLVERR_SESSION_IDLE` 300 -> 3600: not applicable. Byparr has no
  sessions, nothing stays open between solves. Cookies live in
  ext-to-torznab's own `requests.Session` and are reused until Cloudflare
  rejects them.
- "using FlareSolverr for the next 5 min" window: leave at 300s. It is a
  back-off after two failed direct attempts, and `_import_browser_state`
  already resets it to zero the instant a solve returns cookies. Making it
  longer would route more traffic through the browser, not less. Cookie
  lifetime is decided by Cloudflare, not by this timer.
- Egress IP consistency: unchanged. gluetun stays a sidecar in the same pod,
  the `gluetun-rotate` CronJob still restarts the whole pod every 6 hours;
  the first challenged request after a rotation pays one Byparr solve.

## Rollout order

1. Push ext-to-torznab 1.3.0 and tag `v1.3.0` so CI publishes
   `ghcr.io/silkepilon/ext-to-torznab:1.3.0`.
2. Then merge the homelab change. Deploying the manifests first would pair
   Byparr with a proxy that still calls `sessions.create` and every
   challenged fetch would fail.
