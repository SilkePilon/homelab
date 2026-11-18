#!/usr/bin/env bash
# Copy a hand-made Kubernetes Secret into Infisical and prove the copy is exact.
#
#   scripts/infisical-migrate.sh <namespace> <secret> <infisical-folder>
#   scripts/infisical-migrate.sh pihole pihole-secret /pihole
#
# 1. Logs in with ESO's own machine identity (external-secrets/infisical-universal-auth).
# 2. Creates the folder (and parents) in homelab / prod if missing.
# 3. Writes every key of the Secret. A key that already exists is left alone
#    and only compared, so re-running is safe.
# 4. Reads every key back and compares it with the cluster value.
# 5. Applies a throwaway ExternalSecret <secret>-esotest, checks the Secret it
#    produces is byte-identical to the original, and deletes it again.
#
# Values are never printed. Exit code is non-zero on any mismatch.
#
# Exists because PushSecret is broken for Infisical in ESO 2.15.0
# (https://github.com/external-secrets/external-secrets/issues/7324).
set -euo pipefail

NS=${1:?namespace}; SECRET=${2:?secret name}; FOLDER=${3:?infisical folder, e.g. /pihole}
HOST=${INFISICAL_HOST:-https://eu.infisical.com}
PROJECT_SLUG=${INFISICAL_PROJECT:-homelab}
ENV_SLUG=${INFISICAL_ENV:-prod}

auth() { kubectl -n external-secrets get secret infisical-universal-auth -o jsonpath="{.data.$1}" | base64 -d; }

export HOST PROJECT_SLUG ENV_SLUG FOLDER
export CLIENT_ID; CLIENT_ID=$(auth clientId)
export CLIENT_SECRET; CLIENT_SECRET=$(auth clientSecret)
export SECRET_JSON; SECRET_JSON=$(kubectl -n "$NS" get secret "$SECRET" -o json)

python3 - <<'PY'
import base64, json, os, sys, urllib.request, urllib.error, urllib.parse

H = os.environ["HOST"]
def call(method, path, body=None, token=None):
    req = urllib.request.Request(H + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "User-Agent": "homelab-infisical-migrate/1.0",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try: return e.code, json.load(e)
        except Exception: return e.code, {}

st, d = call("POST", "/api/v1/auth/universal-auth/login",
             {"clientId": os.environ["CLIENT_ID"], "clientSecret": os.environ["CLIENT_SECRET"]})
if st != 200: sys.exit(f"login failed: {st} {d.get('message')}")
tok = d["accessToken"]

st, d = call("GET", "/api/v1/workspace", token=tok)
pid = next(w["id"] for w in d["workspaces"] if w["slug"] == os.environ["PROJECT_SLUG"])
env = os.environ["ENV_SLUG"]

# Folders, one segment at a time. 400 = already exists.
parent = "/"
for seg in [s for s in os.environ["FOLDER"].split("/") if s]:
    st, d = call("POST", "/api/v1/folders", {"workspaceId": pid, "projectId": pid,
                 "environment": env, "name": seg, "path": parent}, tok)
    if st not in (200, 400): sys.exit(f"folder {parent}{seg}: {st} {d.get('message')}")
    parent = parent.rstrip("/") + "/" + seg
path = parent

data = {k: base64.b64decode(v).decode() for k, v in json.loads(os.environ["SECRET_JSON"])["data"].items()}
q = lambda k: f"/api/v3/secrets/raw/{urllib.parse.quote(k)}?" + urllib.parse.urlencode(
    {"workspaceId": pid, "environment": env, "secretPath": path})

bad = 0
for k, v in data.items():
    st, d = call("GET", q(k), token=tok)
    if st == 200:
        state = "exists"
    else:
        st, d = call("POST", f"/api/v3/secrets/raw/{urllib.parse.quote(k)}", {"workspaceId": pid,
                     "environment": env, "secretPath": path, "secretValue": v, "type": "shared"}, tok)
        if st != 200: sys.exit(f"write {k}: {st} {d.get('message')}")
        state = "created"
    st, d = call("GET", q(k), token=tok)
    ok = st == 200 and d["secret"]["secretValue"] == v
    bad += not ok
    print(f"  {path}/{k}: {state}, readback {'identical' if ok else 'MISMATCH'}")
if bad: sys.exit(f"{bad} key(s) differ in Infisical")
PY

TEST="$SECRET-esotest"
kubectl apply -f - >/dev/null <<EOF
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata: {name: $TEST, namespace: $NS}
spec:
  refreshInterval: 1h
  secretStoreRef: {kind: ClusterSecretStore, name: infisical}
  target: {name: $TEST, creationPolicy: Owner}
  dataFrom:
    - find: {path: "$FOLDER", name: {regexp: ".*"}}
EOF
trap 'kubectl -n "$NS" delete externalsecret "$TEST" --ignore-not-found >/dev/null' EXIT
kubectl -n "$NS" wait externalsecret "$TEST" --for=condition=Ready --timeout=60s >/dev/null

norm() { kubectl -n "$NS" get secret "$1" -o json | python3 -c 'import json,sys;print(json.dumps(json.load(sys.stdin)["data"],sort_keys=True))'; }
if [ "$(norm "$SECRET")" = "$(norm "$TEST")" ]; then
  echo "  ExternalSecret test: identical to $NS/$SECRET"
else
  echo "  ExternalSecret test: MISMATCH with $NS/$SECRET" >&2; exit 1
fi
