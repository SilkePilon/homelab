# ── Argo CD Application — <APP_NAME> ──────────────────────────────────────────
# Copy this file to bootstrap/argocd/applications/<APP_NAME>.yaml and replace
# (the .tpl suffix keeps the root app-of-apps from trying to apply the
# placeholders as a real Application — the copy must end in .yaml)
# every <PLACEHOLDER>. Then commit and push — Argo CD will pick it up
# automatically (it watches this directory via the app-of-apps root.yaml).
#
# The App-of-Apps root:
#   bootstrap/argocd/root.yaml  ──watches──▶  bootstrap/argocd/applications/
# ─────────────────────────────────────────────────────────────────────────────
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: <APP_NAME>
  namespace: argocd
  # Argo CD will delete its own Application resource if the file is removed
  # from Git. It will NOT delete the deployed resources unless prune: true
  # is also set. Leave this in place for normal operation.
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  source:
    repoURL: https://github.com/SilkePilon/homelab
    targetRevision: main
    path: apps/<APP_NAME>       # ← matches the directory under apps/
  destination:
    server: https://kubernetes.default.svc
    namespace: <APP_NAME>       # ← must exist before Argo CD can sync, but
                                #   CreateNamespace below handles that
  syncPolicy:
    automated:
      prune: true       # delete K8s resources removed from Git
      selfHeal: true    # re-apply if someone manually modifies the cluster
    syncOptions:
      - CreateNamespace=true    # creates the namespace if it doesn't exist
      - ServerSideApply=true    # avoids annotation-size limits on large CRDs
