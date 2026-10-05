#!/usr/bin/env bash
# Everything that can be checked without an AWS account or a GPU.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> terraform fmt, init, validate"
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform init -backend=false -input=false >/dev/null
terraform -chdir=terraform validate

echo "==> render and schema-check the Kubernetes manifests"
export CLUSTER_NAME=example KARPENTER_NODE_ROLE=example-karpenter-node ARTIFACTS_BUCKET=example-bucket
export TRAINER_IMAGE=123456789012.dkr.ecr.us-west-2.amazonaws.com/example:latest RUN_ID=20260101000000
CRDS='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
{
  cat k8s/00-ml-namespace.yaml; echo ---
  for f in k8s/karpenter/*.yaml k8s/training/job.yaml k8s/eval/job.yaml; do envsubst < "$f"; echo ---; done
  kubectl kustomize k8s/serving/base; echo ---
  kubectl kustomize k8s/serving/overlays/lora | envsubst
} > "${TMPDIR:-/tmp}/gemma-karpenter-all.yaml"
# No -ignore-missing-schemas: a resource without a schema should fail the check, not slip past it.
kubeconform -strict -summary -schema-location default -schema-location "$CRDS" "${TMPDIR:-/tmp}/gemma-karpenter-all.yaml"

echo "==> unit tests"
python3 -m pytest tests -q
