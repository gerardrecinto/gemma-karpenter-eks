SHELL := /bin/bash
TF := terraform -chdir=terraform
RUN_ID ?= $(shell date +%Y%m%d%H%M%S)

# Values read from Terraform once the cluster exists.
out = $(shell $(TF) output -raw $(1))

.PHONY: help validate init apply kubeconfig karpenter hf-secret serve serve-lora image train eval eval-job teardown

help:
	@grep -E '^# make ' Makefile | sed 's/^# //'

# make validate     lint Terraform and manifests and run the unit tests, no AWS needed
validate:
	scripts/validate.sh

# make init         download Terraform providers and modules
init:
	$(TF) init

# make apply        create the VPC, EKS, Karpenter, add-ons and bucket (costs money)
apply:
	$(TF) apply

# make kubeconfig   point kubectl at the new cluster
kubeconfig:
	$(call out,kubeconfig_command) | bash

# make karpenter    apply the namespace, NodeClasses and NodePools
karpenter:
	kubectl apply -f k8s/00-ml-namespace.yaml
	CLUSTER_NAME=$(call out,cluster_name) KARPENTER_NODE_ROLE=$(call out,karpenter_node_role_name) \
	  bash -c 'for f in k8s/karpenter/*.yaml; do envsubst < $$f | kubectl apply -f -; done'

# make hf-secret HF_TOKEN=hf_xxx   store the Hugging Face token (accept the Gemma license on huggingface.co first)
hf-secret:
	@test -n "$(HF_TOKEN)" || (echo "pass HF_TOKEN=hf_..." && exit 1)
	kubectl -n ml create secret generic hf-token --from-literal=token=$(HF_TOKEN) --dry-run=client -o yaml | kubectl apply -f -

# make serve        serve the base Gemma model with vLLM, scaled by KEDA
serve:
	kubectl apply -k k8s/serving/base

# make serve-lora   serve the base model and the tuned adapter together (run after make train)
serve-lora:
	ARTIFACTS_BUCKET=$(call out,artifacts_bucket) bash -c 'kubectl kustomize k8s/serving/overlays/lora | envsubst | kubectl apply -f -'

# make image        build and push the training image to ECR
image:
	aws ecr get-login-password --region $(call out,region) | docker login --username AWS --password-stdin $(call out,trainer_image_repository)
	docker build --platform linux/amd64 -t $(call out,trainer_image_repository):latest train
	docker push $(call out,trainer_image_repository):latest

# make train        run one LoRA fine-tune as a Kubernetes Job
train:
	RUN_ID=$(RUN_ID) ARTIFACTS_BUCKET=$(call out,artifacts_bucket) TRAINER_IMAGE=$(call out,trainer_image_repository):latest \
	  bash -c 'envsubst < k8s/training/job.yaml | kubectl apply -f -'
	@echo "follow it with: kubectl -n ml logs -f job/gemma-lora-$(RUN_ID)"

# make eval         compare base and tuned on the held-out prompts (port-forwards the service)
eval:
	aws s3 cp s3://$(call out,artifacts_bucket)/adapters/latest/heldout.jsonl /tmp/heldout.jsonl
	kubectl -n ml port-forward svc/gemma 8000:80 & pid=$$!; sleep 5; \
	  python3 eval/compare.py --endpoint http://localhost:8000 --heldout /tmp/heldout.jsonl --out eval-results.jsonl; \
	  kill $$pid

# make eval-job     run the same comparison as a Job on a Graviton (arm64) node, no port-forward
eval-job:
	kubectl -n ml create configmap eval-script --from-file=eval/compare.py --dry-run=client -o yaml | kubectl apply -f -
	RUN_ID=$(RUN_ID) ARTIFACTS_BUCKET=$(call out,artifacts_bucket) bash -c 'envsubst < k8s/eval/job.yaml | kubectl apply -f -'
	@echo "follow it with: kubectl -n ml logs -f job/gemma-eval-$(RUN_ID)"

# make teardown     delete workloads and NodePools first, then destroy the infrastructure
# Deleting the NodePools makes Karpenter terminate the nodes it launched. If they
# are still running, the VPC cannot be deleted and terraform destroy hangs.
teardown:
	-kubectl delete -k k8s/serving/base --ignore-not-found
	-kubectl -n ml delete jobs --all --ignore-not-found
	-kubectl delete nodepool --all --ignore-not-found
	-kubectl wait --for=delete nodeclaim --all --timeout=600s
	$(TF) destroy
