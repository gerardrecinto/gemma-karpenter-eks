# gemma-karpenter-eks

Serve and fine-tune a Gemma model on Amazon EKS. Karpenter provisions x86 GPU
nodes and Graviton (arm64) CPU nodes just in time, taints, tolerations and node
affinity keep each workload on the architecture it was built for, and KEDA scales
the model server on load.

It answers two questions with working configuration:

1. How do you serve and train an open LLM on Kubernetes, and know whether the
   training helped?
2. Why Karpenter instead of Cluster Autoscaler, and what does each choice cost?
3. How do you run arm64 (Graviton) and x86 nodes side by side without a pod ever
   landing on the wrong one?

The reasoning for all three is in [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md).

## Status

**Not deployed.** What has been checked, and what has not:

| Checked | How |
| :--- | :--- |
| Terraform is valid and formatted | `terraform validate`, `terraform fmt -check` |
| Every Kubernetes manifest matches its schema, including the Karpenter and KEDA resources | `kubeconform -strict`, with no schemas skipped |
| Training data split, chat formatting, S3 upload helper | Unit tests in `tests/` |
| Scheduling rules: GPU pods require amd64 and the GPU pool, Graviton pods tolerate the taint and require arm64, each pool is one architecture | `tests/test_scheduling.py`, shown to fail when a rule is broken on purpose |
| Eval pipeline end to end | A test that runs `eval/compare.py` against a local fake of the vLLM endpoint |
| Helm chart versions and the vLLM image tag exist | Looked up, not run |
| Which architectures each image is published for (vLLM and the training base are amd64 only) | `docker manifest inspect`, on the day this was written |

| Not checked | |
| :--- | :--- |
| Anything on AWS or a GPU | No `terraform apply`, no pod has run |
| The model fits and serves with this vLLM version | Untested |
| The scaling threshold, startup time, cost | No measurements exist. Do not quote numbers for them. |
| The training run improves the model | Untested. The eval exists to find out. |

Run `make validate` to repeat the first group on your machine. Fill in the second
group yourself after a real run, with the numbers you measured.

## How it fits together

```
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

| Path | What it is |
| :--- | :--- |
| `terraform/` | VPC, EKS, Karpenter and its IAM and queue, NVIDIA device plugin, Prometheus, KEDA, the S3 bucket, ECR repo and pod identity |
| `k8s/karpenter/` | Two `EC2NodeClass` and three `NodePool`: `cpu` (amd64), `graviton` (arm64, tainted) and `gpu` (amd64, tainted, GPU limit) |
| `k8s/serving/` | vLLM Deployment, Service and KEDA `ScaledObject`, plus an overlay that adds the tuned LoRA adapter |
| `k8s/training/` | The LoRA fine-tune Job (GPU, amd64) |
| `k8s/eval/` | The eval Job, which runs on Graviton (arm64) |
| `train/` | Training script and its Dockerfile |
| `eval/` | Compares base and tuned answers on held-out prompts |
| `docs/DESIGN_DECISIONS.md` | The reasoning, trade-offs and known gaps |

## Before you start

- An AWS account, credentials for it, and quota for `g5` or `g6` instances in your
  region (GPU quota is zero by default on new accounts and takes a request to raise).
- `terraform` 1.5 or newer, `kubectl`, `helm`, `aws`, `docker`, `envsubst`.
- A Hugging Face account that has accepted the Gemma license on the model page,
  and a read token.

## Cost warning

`make apply` creates billable resources: an EKS control plane, two always-on
nodes, a NAT gateway, and (once you run the workloads) GPU instances. There is no
estimate here because none has been measured. Check current prices for your
region, run `make teardown` when you finish, and set a billing alarm first.

## Run it

```
make init
make apply
make kubeconfig
make karpenter          # namespace, NodeClasses, NodePools
make hf-secret HF_TOKEN=hf_xxx
make serve              # base Gemma behind vLLM
```

The first request waits for a GPU node to launch and the model to load. Watch it:

```
kubectl get nodeclaims -w
kubectl -n ml get pods -w
kubectl -n ml logs deploy/gemma -f
```

Then train and compare:

```
make image              # build and push the training image
make train              # a Kubernetes Job on a GPU node
kubectl -n ml logs -f job/gemma-lora-<RUN_ID>
make serve-lora         # base model and tuned adapter, one server
make eval               # prints the comparison, writes eval-results.jsonl
make eval-job           # the same, as a Job on a Graviton node
```

Read `eval-results.jsonl` next to the printed summary. The score is word overlap
with the reference answer, which can reward a longer answer for the wrong reason.

To see Karpenter work, send load and watch the replica count and nodes rise, then
stop and watch the GPU node disappear after the cooldown and `consolidateAfter`.

## Tear it down

```
make teardown
```

It removes the workloads and the NodePools first so Karpenter terminates the
nodes it launched, then runs `terraform destroy`. Skipping that order leaves
instances behind that block the VPC from deleting.

## Limits

See the "Known gaps" section of the design document. In short: it has not run,
the eval metric is crude, training does not checkpoint, the API endpoint is
public, and the image architecture check is a snapshot that needs repeating
when an image version changes.
