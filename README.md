<div align="center">

# gemma-karpenter-eks

**Serve and fine-tune Gemma on EKS, with Karpenter, Graviton and GPU node pools, vLLM and KEDA.**

[Design decisions](docs/DESIGN_DECISIONS.md) · [Status](#what-is-verified-not-run-and-not-claimed) · [Try it without AWS](#try-it-in-five-minutes-no-aws-no-gpu) · [Run it locally](#run-it-on-a-local-cluster-no-aws-no-gpu) · [Terragrunt](#environments-with-terragrunt-shared-variables-written-once) · [Pulumi](#the-storage-layer-in-pulumi-go-and-python) · [Run it on AWS](#run-it-on-aws)

[![CI](https://github.com/gerardrecinto/gemma-karpenter-eks/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/gerardrecinto/gemma-karpenter-eks/actions/workflows/ci.yml)
![Status](https://img.shields.io/badge/status-validated%20locally%2C%20not%20on%20AWS-yellow)
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

## Run it on a local cluster (no AWS, no GPU)

`make validate` only reads files. `make local` actually deploys: it creates a four-node kind cluster, labels and taints three workers like the three NodePools, installs Prometheus and KEDA from the same chart versions Terraform pins, applies the real serving and eval manifests through a small overlay in `k8s/local/`, and exercises them. You need `docker`, `kind`, `kubectl`, `helm` and `envsubst`, on an arm64 host (Apple silicon or arm64 Linux), because the eval Job requires arm64 and kind nodes take the host's architecture.

```bash
make local        # a few minutes the first time
make local-down   # delete the cluster
```

What it checks, and what came out on an Apple silicon laptop:

| Step | Result |
| :--- | :--- |
| The Deployment lands on the node standing in for the GPU pool | Scheduled to the node labelled `gpu`, whose taint it tolerates |
| A pod with no toleration is refused by the tainted pools | Stayed `Pending`: `3 node(s) had untolerated taint(s)` |
| KEDA scales on the Prometheus queue query | Queue set to 20: 1 to 3 replicas in about 80 seconds, most of it Prometheus's default one-minute scrape interval |
| Scale back down | Queue set to 0: 3 to 1 replicas in about 70 seconds, with the overlay's 30 second window (the default window is 300 seconds) |
| The eval Job runs on the Graviton pool | Completed on the node labelled `graviton`, which is arm64, and printed the side-by-side summary |

The model server is a stub (`local/stub/server.py`) that serves `/health`, a canned chat answer, and the two metric names vLLM exports. It is not vLLM and serves no model. It exists so the cluster plumbing is real while the model is not.

The first local run found two real problems, now fixed: the `ScaledObject` set `pollingInterval` and `cooldownPeriod`, which KEDA ignores when the minimum is 1 (the scale-down delay is the autoscaler's own window), and the docs described scale-down as waiting for a cooldown that never applied.

## Environments with Terragrunt (shared variables written once)

`live/` runs the same `terraform/` stack once per environment without copying it. `live/common.hcl` holds what every environment shares (region, cluster version, base tags), `live/root.hcl` holds the remote state and the module source, and each environment's `terragrunt.hcl` sets only what differs: its cluster name and an `environment` tag.

```text
live/
  common.hcl        region, cluster version, base tags
  root.hcl          S3 backend (key per environment) and the terraform/ source
  dev/terragrunt.hcl    cluster_name = gemma-karpenter-dev
  prod/terragrunt.hcl   cluster_name = gemma-karpenter-prod
```

Check both environments without an AWS account (state stays in a local file):

```bash
make terragrunt-validate     # hcl fmt --check, then terragrunt run --all -- validate
```

For a real apply, set `TG_STATE_BUCKET` and `TG_LOCK_TABLE` to a bucket and DynamoDB table you own, unset `TG_LOCAL_STATE`, and run `terragrunt apply` inside `live/dev`. State is not shared between environments, because the key includes the path. Trade-off: one more tool to install, and the two environments differ only by name and tag, so today this buys the pattern more than it buys different settings.

## The storage layer in Pulumi (Go and Python)

`pulumi/` holds the same storage layer as `terraform/storage.tf` written twice, once in Go and once in Python: the versioned, encrypted, private artifacts bucket, the trainer ECR repository, and the pod identity role that lets the `ml-workload` service account read and write the bucket. Only this slice is ported. The VPC, EKS, Karpenter and add-ons stay in Terraform, so Pulumi here shows how the same resources read in each language, not a second way to build the cluster.

Each program has eight unit tests that run it against Pulumi's mock engine, so they need no AWS account and no Pulumi login: bucket name, versioning, all four public-access blocks, AES256, the trust policy, a policy with no wildcard resource, the service account binding, and scan on push. Both suites were shown to fail when a setting was changed on purpose.

```bash
make pulumi-test             # pytest for Python, go test for Go
```

On macOS, if the Go linker fails on the system SDK, build with `CGO_ENABLED=0`, which `make pulumi-test` already does.

## What is verified, not run, and not claimed

| Status | What |
| :--- | :--- |
| Implemented, with tests | Scheduling rules, eval pipeline, training data split and chat formatting, S3 upload helper. The Pulumi storage layer in Go and Python, eight tests each, against Pulumi's mocks. |
| Validated statically | `terraform validate` and `terraform fmt -check`. `terragrunt run --all -- validate` for both environments, with local state. `kubeconform -strict` on all 15 rendered resources. Helm chart versions and the vLLM image tag were looked up and exist. |
| Run locally | Serving and eval manifests on a kind cluster with Prometheus and KEDA: scaling on the queue metric, taint and affinity placement, eval Job on an arm64 node. The model server is a stub, so no model is served. See above. |
| Checked once, needs repeating | Which architectures each image is published for. The vLLM image and the training base are amd64 only, checked with `docker manifest inspect` on the day this was written. |
| Not run | `terraform apply`, `terragrunt apply`, `pulumi preview` or `pulumi up` against AWS, Karpenter, EC2, any GPU node, real vLLM. Whether the model fits and serves with this vLLM version. The scaling threshold under real load, startup time and cost. The training run improving the model. |
| Not claimed | Any cost, latency or throughput figure for real serving. The local timings above measure the autoscaler against a stub, not a model. The eval exists to find out whether training helped. |

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

Read `eval-results.jsonl` next to the printed summary. To see Karpenter work, send load and watch the replica count and nodes rise, then stop and watch the replicas fall after the autoscaler's scale-down window (300 seconds by default) and the GPU node disappear after `consolidateAfter`.

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
| `live/` | Terragrunt: shared variables, remote state and one directory per environment |
| `pulumi/go/`, `pulumi/python/` | The storage layer in Pulumi, with mock-based unit tests |
| `scripts/validate.sh` | Everything that can be checked without an AWS account or a GPU |
| `local/`, `k8s/local/` | The kind cluster config, the stub model server, `run.sh`, and the overlays that adapt the real manifests to a laptop |
| `docs/DESIGN_DECISIONS.md` | The reasoning, trade-offs and known gaps |
