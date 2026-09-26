# NetCopilot Production-Ready Kubernetes Variant

This folder provides a hardened baseline for production-like deployments.

## Build image
```bash
docker build -t netcopilot:prod -f Dockerfile.prod .
```

## Apply manifests
```bash
kubectl apply -f deploy/k8s/prod/namespace.yaml
kubectl apply -f deploy/k8s/prod/configmap.yaml
kubectl apply -f deploy/k8s/prod/secret.yaml
kubectl apply -f deploy/k8s/prod/devices-configmap.yaml
kubectl apply -f deploy/k8s/prod/pvc.yaml
kubectl apply -f deploy/k8s/prod/serviceaccount.yaml
kubectl apply -f deploy/k8s/prod/deployment.yaml
kubectl apply -f deploy/k8s/prod/service.yaml
kubectl apply -f deploy/k8s/prod/networkpolicy.yaml
kubectl apply -f deploy/k8s/prod/ingress.yaml
```

## Update secrets
```bash
kubectl -n netcopilot-prod edit secret netcopilot-secrets
```

## Inventory updates
Update `deploy/k8s/prod/devices-configmap.yaml`, re-apply:
```bash
kubectl apply -f deploy/k8s/prod/devices-configmap.yaml
```
Then call:
```bash
curl -sS -X POST -H "X-API-Key: $NETCOPILOT_ADMIN_KEY" https://netcopilot.example.com/admin/reload-inventory
```

## Notes
- This assumes an ingress controller (e.g., nginx) and a TLS secret.
- Single replica with `Recreate` rollouts: state (settings, audit trail,
  investigation history) is SQLite on a ReadWriteOnce volume. Running more
  replicas requires moving that state to an external database first.
- Guardrails are locked (`NETCOPILOT_LOCK_GUARDRAILS=true`): security settings
  come only from the ConfigMap/Secret and cannot be changed in the Admin UI.
- The container runs with a read-only root filesystem; writable paths are the
  data volume (`/app/data`) and `/tmp`. Keep `LOG_TO_FILE` unset (logs go to stdout).
- Review the Admin → Posture page after deploying; it lists any guardrail still off.
