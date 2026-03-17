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
kubectl apply -f deploy/k8s/prod/pdb.yaml
kubectl apply -f deploy/k8s/prod/hpa.yaml
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
curl -sS -X POST https://netcopilot.example.com/admin/reload-inventory
```

## Notes
- This assumes an ingress controller (e.g., nginx) and a TLS secret.
- For production, use external secret manager and external DB (Postgres).
