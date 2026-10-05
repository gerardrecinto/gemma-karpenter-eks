#!/usr/bin/env bash
# Deploy the serving and eval manifests to a local kind cluster and exercise them.
# Needs: docker, kind, kubectl, helm, envsubst. No AWS account and no GPU.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

CLUSTER=gemma-local
CTX=kind-$CLUSTER
k() { kubectl --context "$CTX" "$@"; }

echo "==> cluster"
kind get clusters | grep -x "$CLUSTER" >/dev/null || kind create cluster --name "$CLUSTER" --config local/kind-config.yaml

echo "==> label and taint the workers like the three NodePools"
workers=($(k get nodes -l '!node-role.kubernetes.io/control-plane' -o name | sed 's|node/||'))
k label node "${workers[0]}" karpenter.sh/nodepool=cpu --overwrite
k label node "${workers[1]}" karpenter.sh/nodepool=graviton karpenter.sh/capacity-type=spot --overwrite
k taint node "${workers[1]}" arch=arm64:NoSchedule --overwrite
k label node "${workers[2]}" karpenter.sh/nodepool=gpu --overwrite
k taint node "${workers[2]}" nvidia.com/gpu=true:NoSchedule --overwrite

echo "==> stub image"
docker build -q -t vllm-stub:local local/stub
kind load docker-image vllm-stub:local --name "$CLUSTER"

echo "==> Prometheus and KEDA, same charts and versions as terraform/addons.tf"
PROM_VERSION=$(grep -A3 'variable "prometheus_version"' terraform/variables.tf | sed -n 's/.*default *= *"\(.*\)"/\1/p')
KEDA_VERSION=$(grep -A3 'variable "keda_version"' terraform/variables.tf | sed -n 's/.*default *= *"\(.*\)"/\1/p')
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add kedacore https://kedacore.github.io/charts >/dev/null
helm repo update >/dev/null
helm --kube-context "$CTX" upgrade --install prometheus prometheus-community/prometheus -n monitoring --create-namespace \
  --version "$PROM_VERSION" --wait --timeout 5m \
  --set alertmanager.enabled=false --set prometheus-pushgateway.enabled=false \
  --set prometheus-node-exporter.enabled=false --set kube-state-metrics.enabled=false \
  --set server.persistentVolume.enabled=false
helm --kube-context "$CTX" upgrade --install keda kedacore/keda -n keda --create-namespace \
  --version "$KEDA_VERSION" --wait --timeout 5m

echo "==> serving (real manifests through the local overlay)"
k apply -f k8s/00-ml-namespace.yaml
k -n ml create secret generic hf-token --from-literal=token=local-not-a-real-token --dry-run=client -o yaml | k apply -f -
k apply -k k8s/local/serving
k -n ml rollout status deploy/gemma --timeout=180s
k -n ml get pods -o wide

echo "==> placement: a pod with no toleration must not land on the GPU or Graviton node"
k -n ml delete pod intruder --ignore-not-found >/dev/null
k -n ml run intruder --image=busybox:1.36 --restart=Never --overrides='{"spec":{"nodeSelector":{"karpenter.sh/nodepool":"gpu"}}}' -- sleep 60
sleep 8
k -n ml get pod intruder
k -n ml describe pod intruder | grep -A2 "FailedScheduling" | head -4
k -n ml delete pod intruder --wait=false >/dev/null

echo "==> autoscaling: put 20 requests in the queue and watch KEDA add replicas"
k -n ml port-forward deploy/gemma 18000:8000 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
sleep 3
curl -s -X POST localhost:18000/load -d '{"waiting": 20}'; echo
for i in $(seq 1 24); do
  r=$(k -n ml get deploy gemma -o jsonpath='{.status.readyReplicas}')
  echo "t+$((i*10))s ready replicas: ${r:-0}"
  [ "${r:-0}" -ge 3 ] && break
  sleep 10
done
k -n ml get scaledobject gemma
k -n ml get pods -o wide

echo "==> drain the queue and watch it scale back"
curl -s -X POST localhost:18000/load -d '{"waiting": 0}'; echo
for i in $(seq 1 24); do
  r=$(k -n ml get deploy gemma -o jsonpath='{.status.replicas}')
  echo "t+$((i*10))s replicas: ${r:-0}"
  [ "${r:-0}" -le 1 ] && break
  sleep 10
done

echo "==> eval Job on the Graviton-labelled node"
k -n ml create configmap eval-script --from-file=eval/compare.py --dry-run=client -o yaml | k apply -f -
RUN_ID=local ARTIFACTS_BUCKET=none bash -c 'kubectl kustomize k8s/local/eval | envsubst' | k apply -f -
k -n ml wait --for=condition=complete job/gemma-eval-local --timeout=180s
k -n ml get pods -l job-name=gemma-eval-local -o wide
k -n ml logs job/gemma-eval-local
