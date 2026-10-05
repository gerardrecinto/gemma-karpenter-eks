<div align="center">

# gemma-karpenter-eks

**Serve and fine-tune Gemma on EKS, with Karpenter, Graviton and GPU node pools, vLLM and KEDA.**

[Design decisions](docs/DESIGN_DECISIONS.md) · [Status](#what-is-verified-not-run-and-not-claimed) · [Try it without AWS](#try-it-in-five-minutes-no-aws-no-gpu) · [Run it on AWS](#run-it-on-aws)

[![CI](https://github.com/gerardrecinto/gemma-karpenter-eks/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/gerardrecinto/gemma-karpenter-eks/actions/workflows/ci.yml)
![Status](https://img.shields.io/badge/status-validated%2C%20not%20deployed-yellow)
![Terraform](https://img.shields.io/badge/Terraform-%E2%89%A51.5-7B42BC?logo=terraform&logoColor=white)
![Kubernetes](https://img.shields.io/badge/Kubernetes-EKS-326CE5?logo=kubernetes&logoColor=white)

</div>

This is the configuration for running an open LLM on Kubernetes without paying for idle GPUs and without a pod landing on the wrong CPU architecture. Karpenter launches x86 GPU nodes and Graviton (arm64) CPU nodes just in time. Taints, tolerations and node affinity keep each workload on the architecture its image was built for. KEDA scales the model server on queue depth, and a LoRA fine-tune plus an eval tell you whether the training helped.

**The problem.** GPU nodes are expensive, slow to start and easy to waste. A Cluster Autoscaler setup needs one node group per instance type you might want. A mixed x86 and arm64 cluster fails quietly when an amd64 image reaches an arm64 node: `exec format error` and a crash loop. Scaling a GPU server on CPU hides the load, because the GPU saturates while the CPU idles.

**What this repo does.** One Terraform stack builds the cluster, Karpenter, the NVIDIA device plugin, Prometheus and KEDA. Three NodePools split the demand: `cpu`, `graviton` (tainted `arch=arm64`) and `gpu` (tainted `nvidia.com/gpu`, capped at 4 GPUs). The GPU pool removes only empty nodes, because moving a pod means reloading the model. Every decision, what it was chosen over and what it costs, is written down in [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md).

```text
            requests
               |
        Service gemma  ---->  vLLM pod (GPU)  --- /metrics ---> Prometheus
               ^                  ^                                  |
               |                  | needs 1 GPU                      | queue depth
      scales replicas             |                                  v
               +------------- KEDA ScaledObject <--------------------+
                                  |
                  pod is Pending, asks for nvidia.com/gpu
                                  |
                                  v
                Karpenter launches a g5 or g6 node (spot first)
                                  |
              NVIDIA device plugin advertises the GPU, pod starts

   Pool        arch    taint              runs
   cpu         amd64   none               default CPU workloads
   graviton    arm64   arch=arm64         eval Job (multi-arch images only)
   gpu         amd64   nvidia.com/gpu     vLLM and training (amd64-only images)

   Training Job ---> GPU node ---> adapter + held-out prompts ---> S3
                                                                    |
   vLLM init container <---- s3 sync adapters/latest <--------------+
```

## Try it in five minutes (no AWS, no GPU)

You need `terraform` 1.5 or newer, `kubectl`, `kubeconform`, `envsubst` and Python 3 with `pytest` and `pyyaml`. Nothing here touches AWS:

```bash
git clone https://github.com/gerardrecinto/gemma-karpenter-eks && cd gemma-karpenter-eks
make validate
```

The first run downloads the Terraform providers and modules, and took about a minute and a half on my machine. Terraform, the manifests and the tests all have to pass:

```text
==> terraform fmt, init, validate
Success! The configuration is valid.
==> render and schema-check the Kubernetes manifests
Summary: 15 resources found in 1 file - Valid: 15, Invalid: 0, Errors: 0, Skipped: 0
==> unit tests
31 passed, 3 skipped in 1.52s
```

The same command runs in CI on every push and pull request. The 3 skipped tests are scheduling checks that do not apply to a manifest, for example a Deployment that does not ask for a GPU.

| What the check enforces | Where |
| :--- | :--- |
| Every manifest, including the Karpenter and KEDA resources, matches its schema. No schema is skipped, so a resource without one fails the check. | `scripts/validate.sh`, `kubeconform -strict` |
| A GPU pod requires amd64 and the GPU pool. A Graviton pod tolerates the taint and requires arm64. Each pool is one architecture. | `tests/test_scheduling.py`, shown to fail when a rule is broken on purpose |
| The eval pipeline runs end to end against a local fake of the vLLM endpoint. | `tests/test_eval_compare.py` |
| The training data split is deterministic and the held-out prompts never overlap the training prompts. | `tests/test_train_helpers.py` |

## What is verified, not run, and not claimed

| Status | What |
| :--- | :--- |
| Implemented, with tests | Scheduling rules, eval pipeline, training data split and chat formatting, S3 upload helper. |
| Validated statically | `terraform validate` and `terraform fmt -check`. `kubeconform -strict` on all 15 rendered resources. Helm chart versions and the vLLM image tag were looked up and exist. |
| Checked once, needs repeating | Which architectures each image is published for. The vLLM image and the training base are amd64 only, checked with `docker manifest inspect` on the day this was written. |
| Not run | `terraform apply`, any pod, any GPU. Whether the model fits and serves with this vLLM version. The scaling threshold, startup time and cost. The training run improving the model. |
| Not claimed | Any measured number. There is no cost estimate, latency or throughput figure because none exists. The eval exists to find out whether training helped. |

**Limits.** The eval score is word overlap with the reference answer, which can reward a longer answer for the wrong reason. Training loss covers the prompt as well as the answer. There is no checkpointing, so a spot interruption restarts training. Prometheus has no persistent storage. The cluster API endpoint is public. The full list is in the "Known gaps" section of [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md).

## The decisions

Each one is written as what was chosen, what it was chosen over, and what it costs.

| Decision | Short version |
| :--- | :--- |
| [Karpenter, not Cluster Autoscaler](docs/DESIGN_DECISIONS.md#1-karpenter-instead-of-cluster-autoscaler) | Autoscaler resizes node groups you defined. Karpenter launches the node the pending pods need. Costs: AWS-specific, its own permissions and queue, consolidation moves pods. |
| [Spot first, on-demand fallback](docs/DESIGN_DECISIONS.md#2-spot-first-on-demand-as-the-fallback) | Cheaper, safe enough with wide instance choice and the interruption queue. The GPU pool is narrower, so the fallback matters more there. |
| [Consolidation differs by pool](docs/DESIGN_DECISIONS.md#3-consolidation-is-different-for-cpu-and-gpu) | CPU repacks. GPU removes only empty nodes, because a moved pod reloads the model for minutes. |
| [Graviton next to x86](docs/DESIGN_DECISIONS.md#4-graviton-arm64-next-to-x86-with-taints-tolerations-and-affinity) | A tainted arm64 pool plus affinity, so no pod lands on the wrong architecture. |
| [Scale on load, not CPU](docs/DESIGN_DECISIONS.md#5-autoscaling-the-model-server-on-load-not-cpu) | Queue depth from vLLM metrics. One warm replica, not zero. The threshold of 8 is a starting point, not a measurement. |
| [Cost guardrails](docs/DESIGN_DECISIONS.md#6-guardrails-against-runaway-cost) | Limits on every pool (4 GPUs on the GPU pool), a taint so only GPU pods land there, and 30-day node expiry. |

## Run it on AWS

You need an AWS account and quota for `g5` or `g6` instances (GPU quota is zero on new accounts and takes a request to raise), `terraform`, `kubectl`, `helm`, `aws`, `docker` and `envsubst`, plus a Hugging Face account that has accepted the Gemma license and a read token.

> **Cost warning.** `make apply` creates billable resources: an EKS control plane, two always-on nodes, a NAT gateway, and GPU instances once the workloads run. No estimate is given because none was measured. Check current prices for your region, set a billing alarm first, and run `make teardown` when you finish.

```bash
make init
make apply
make kubeconfig
make karpenter                    # namespace, NodeClasses, NodePools
make hf-secret HF_TOKEN=hf_xxx
make serve                        # base Gemma behind vLLM
```

The first request waits for a GPU node to launch and the model to load:

```bash
kubectl get nodeclaims -w
kubectl -n ml get pods -w
kubectl -n ml logs deploy/gemma -f
```

Then train and compare:

```bash
make image                        # build and push the training image
make train                        # a Kubernetes Job on a GPU node
kubectl -n ml logs -f job/gemma-lora-<RUN_ID>
make serve-lora                   # base model and tuned adapter, one server
make eval                         # prints the comparison, writes eval-results.jsonl
make eval-job                     # the same, as a Job on a Graviton node
```

Read `eval-results.jsonl` next to the printed summary. To see Karpenter work, send load and watch the replica count and nodes rise, then stop and watch the GPU node disappear after the cooldown and `consolidateAfter`.

**Tear it down with `make teardown`.** It removes the workloads and the NodePools first so Karpenter terminates the nodes it launched, then runs `terraform destroy`. Skipping that order leaves instances behind that block the VPC from deleting.

If you run it, fill in the "Not run" row above with what you measured.

## Repository map

| Path | What it is |
| :--- | :--- |
| `terraform/` | VPC, EKS, Karpenter with its IAM and queue, NVIDIA device plugin, Prometheus, KEDA, the S3 bucket, ECR repo and pod identity |
| `k8s/karpenter/` | Two `EC2NodeClass` and three `NodePool`: `cpu`, `graviton` and `gpu` |
| `k8s/serving/` | vLLM Deployment, Service and KEDA `ScaledObject`, plus an overlay that adds the tuned LoRA adapter |
| `k8s/training/` | The LoRA fine-tune Job (GPU, amd64) |
| `k8s/eval/` | The eval Job, which runs on Graviton (arm64) |
| `train/` | Training script and its Dockerfile |
| `eval/` | Compares base and tuned answers on held-out prompts |
| `tests/` | Scheduling rules, eval pipeline, training helpers |
| `scripts/validate.sh` | Everything that can be checked without an AWS account or a GPU |
| `docs/DESIGN_DECISIONS.md` | The reasoning, trade-offs and known gaps |
