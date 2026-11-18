# external-secrets

Secrets live in **Infisical Cloud** (US region, project `homelab`, slug `homelab-qw0-k`, environment `prod`).
External Secrets Operator (ESO) copies them into normal Kubernetes Secrets, so
the app manifests keep referencing a Secret by name like before.

```text
Infisical  homelab / prod / /n8n/N8N_ENCRYPTION_KEY
   │  (ClusterSecretStore "infisical", refreshed every hour)
   ▼
ExternalSecret n8n/n8n-secret  ──▶  Secret n8n/n8n-secret  ──▶  pod envFrom
```

## One-time setup

1. Sign up at <https://app.infisical.com>, create project **homelab**. Check
   that its slug is `homelab` (Project Settings); if Infisical added a suffix,
   update `projectSlug` in `cluster-secret-store.yaml`.
2. Organization → Access Control → Identities → **Create identity**
   `eso-homelab`, auth method **Universal Auth**. Create a client secret.
3. In the project → Access Control → Machine Identities → add `eso-homelab`
   with role **Developer** (read + write; write is needed for the migration
   below, drop to Viewer afterwards if you like).
4. Create the bootstrap secret:

   ```bash
   kubectl -n external-secrets create secret generic infisical-universal-auth \
     --from-literal=clientId='<client id>' \
     --from-literal=clientSecret='<client secret>'
   ```

5. `kubectl get clustersecretstore infisical` should show `Valid / Ready`.

Keep the client ID and secret in your password manager. On a rebuilt cluster
this is the only secret you create by hand; everything else comes back.

## Using it in an app

Put the keys in a folder named after the app, then add an `externalsecret.yaml`
to the app and list it in its `kustomization.yaml`:

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: n8n-secret
  namespace: n8n
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: infisical
  target:
    name: n8n-secret          # the Secret the Deployment already references
    creationPolicy: Owner
  dataFrom:
    - find:
        path: /n8n            # every key in this Infisical folder
        name:
          regexp: ".*"
```

Change a value in the Infisical dashboard and the Secret updates within the
refresh interval. Pods only see the new value after a restart
(`kubectl -n n8n rollout restart deploy/n8n`).

## Migrating an existing hand-made Secret

The existing Secret can push itself into Infisical with a one-off
`PushSecret`, so nothing is copied by hand. Apply this with kubectl (do **not**
commit it), check the folder in the dashboard, then delete it:

```yaml
apiVersion: external-secrets.io/v1alpha1
kind: PushSecret
metadata:
  name: migrate
  namespace: n8n
spec:
  refreshInterval: 0s           # push once
  updatePolicy: IfNotExists     # never overwrite a value already in Infisical
  deletionPolicy: None          # deleting this PushSecret leaves Infisical alone
  secretStoreRefs:
    - kind: ClusterSecretStore
      name: infisical
  selector:
    secret:
      name: n8n-secret
  data:
    - match:
        secretKey: N8N_ENCRYPTION_KEY
        remoteRef:
          remoteKey: /n8n/N8N_ENCRYPTION_KEY
```

Then commit the app's ExternalSecret. Its target has the same name as the old
Secret, so ESO takes it over (`creationPolicy: Owner` adopts it) and the pods
see the same values.
