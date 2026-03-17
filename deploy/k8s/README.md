# NetCopilot Pilot Kubernetes Deployment

## Build image (local)
```bash
docker build -t netcopilot:pilot .
```

## Apply manifests
```bash
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/secret.yaml
kubectl apply -f deploy/k8s/devices-configmap.yaml
kubectl apply -f deploy/k8s/pvc.yaml
kubectl apply -f deploy/k8s/deployment.yaml
kubectl apply -f deploy/k8s/service.yaml
```

## Optional ingress
```bash
kubectl apply -f deploy/k8s/ingress.yaml
```

## Update secrets
```bash
kubectl -n netcopilot edit secret netcopilot-secrets
```

## Reload device inventory
Update `deploy/k8s/devices-configmap.yaml`, re-apply:
```bash
kubectl apply -f deploy/k8s/devices-configmap.yaml
```
Then call the API (admin only):
```bash
curl -sS -X POST http://<service-url>/admin/reload-inventory
```

## Notes
- SQLite DBs live in `/app/data` backed by `netcopilot-data` PVC.
- For production, use external DB (Postgres) and a secrets manager.
