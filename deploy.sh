#!/usr/bin/env bash
# Deploy The Street Talks Back to the team namespace (no registry: python:3.12-slim + ConfigMap code + Secret env).
# Usage: tools/street-talks-back/deploy.sh            (reads credentials from the already-exported environment)
set -euo pipefail

NS="${USERNAME:?USERNAME (team namespace) must be set}"
TEAM_N="${NS#team-}"
APP_HOST="video-lab-team-${TEAM_N}.cosmos.vastdata.com"
APP_NAME="street-talks-back"
APP_PORT=8080
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
export KUBECONFIG="${KUBECONFIG:-/config/${NS}-k8s.yaml}"

echo "namespace=$NS host=$APP_HOST app=$APP_NAME"

# 1. code
kubectl -n "$NS" create configmap "${APP_NAME}-code" \
  --from-file="$APP_DIR/main.py" --from-file="$APP_DIR/pipeline.py" --from-file="$APP_DIR/store.py" \
  --from-file="$APP_DIR/llm.py" --from-file="$APP_DIR/vss.py" --from-file="$APP_DIR/telemetry.py" --from-file="$APP_DIR/personas.json" \
  --from-file="$APP_DIR/index.html" --from-file="$APP_DIR/app.js" --from-file="$APP_DIR/style.css" \
  --from-file="$APP_DIR/matchmaker-avatar.jpg" \
  --from-file="$APP_DIR/requirements.txt" --from-file="$APP_DIR/README.md" --from-file="$APP_DIR/EVAL.md" \
  --dry-run=client -o yaml | kubectl apply -f -

# 2. env (values come from the current environment; never echoed)
kubectl -n "$NS" create secret generic "${APP_NAME}-env" \
  --from-literal=VSS_URL="${VSS_INTERNAL_URL:-http://video-backend-service.${NS}.svc.cluster.local:8000}" \
  --from-literal=VSS_USERNAME="$USERNAME" \
  --from-literal=VSS_PASSWORD="$PASSWORD" \
  --from-literal=WANDB_API_KEY="$WANDB_API_KEY" \
  --from-literal=WANDB_TEAM="$WANDB_TEAM" \
  --from-literal=WANDB_PROJECT="$WANDB_PROJECT" \
  --from-literal=S3_ENDPOINT="$S3_ENDPOINT" \
  --from-literal=ACCESS_KEY="$ACCESS_KEY" \
  --from-literal=SECRET_KEY="$SECRET_KEY" \
  --from-literal=VASTDB_BUCKET="$VASTDB_BUCKET" \
  --from-literal=WEBHOOK_URL="${WEBHOOK_URL:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

# 3. workload + ingress (path /app on the team host)
kubectl -n "$NS" apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${APP_NAME}
  labels: {app: ${APP_NAME}}
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector: {matchLabels: {app: ${APP_NAME}}}
  template:
    metadata:
      labels: {app: ${APP_NAME}}
    spec:
      containers:
      - name: app
        image: python:3.12-slim
        imagePullPolicy: IfNotPresent
        ports: [{containerPort: ${APP_PORT}}]
        envFrom: [{secretRef: {name: ${APP_NAME}-env}}]
        env:
        - {name: PORT, value: "${APP_PORT}"}
        - {name: DATA_DIR, value: /tmp/street-talks-back}
        - {name: AUTO_SCAN, value: "1"}
        - {name: SCAN_WORKERS, value: "10"}
        - {name: PYTHONUNBUFFERED, value: "1"}
        volumeMounts: [{name: code, mountPath: /code}]
        workingDir: /code
        command: ["bash", "-c"]
        args:
        - |
          set -euo pipefail
          pip install --no-cache-dir -q -r requirements.txt || echo "pip install failed; continuing with JSON store"
          exec python main.py
        readinessProbe:
          httpGet: {path: /health, port: ${APP_PORT}}
          initialDelaySeconds: 20
          periodSeconds: 10
          failureThreshold: 30
        resources:
          requests: {cpu: "250m", memory: "512Mi"}
          limits: {memory: "2Gi"}
      volumes:
      - name: code
        configMap: {name: ${APP_NAME}-code}
---
apiVersion: v1
kind: Service
metadata:
  name: ${APP_NAME}
  labels: {app: ${APP_NAME}}
spec:
  selector: {app: ${APP_NAME}}
  ports: [{name: http, port: 80, targetPort: ${APP_PORT}}]
  type: ClusterIP
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ${APP_NAME}
  labels: {app: ${APP_NAME}}
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /\$2
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
    nginx.ingress.kubernetes.io/proxy-body-size: "20m"
spec:
  ingressClassName: nginx
  rules:
  - host: ${APP_HOST}
    http:
      paths:
      - path: /app(/|$)(.*)
        pathType: ImplementationSpecific
        backend:
          service: {name: ${APP_NAME}, port: {number: 80}}
EOF

kubectl -n "$NS" rollout restart deploy/"$APP_NAME" >/dev/null 2>&1 || true
kubectl -n "$NS" rollout status deploy/"$APP_NAME" --timeout=600s
kubectl -n "$NS" get pods,svc,ingress -l app="$APP_NAME"
echo "Open https://workshop.thecosmoslabs.com and click App."
