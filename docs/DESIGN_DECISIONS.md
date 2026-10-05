# Design decisions

The decisions behind this cluster, written so each one can be explained out loud:
what was chosen, what it was chosen over, and what it costs. Nothing here is a
measured result. This project has not been deployed yet, so where a number would
help, the text says what to measure instead.

## 1. Karpenter instead of Cluster Autoscaler

**The short answer.** Cluster Autoscaler scales node groups. Karpenter launches
nodes. That difference drives everything below.

**How Cluster Autoscaler works.** It watches for pods that cannot be scheduled.
For each one it asks which existing node group, if grown by one node, would fit
the pod, then raises that Auto Scaling group's desired size. It can only choose
among node groups someone defined ahead of time. Each group has one instance type
(or a small set of similar ones), so the cluster's possible node shapes are
decided by the person who wrote the groups, not by the pods that are waiting.

**How Karpenter works.** It also watches for unschedulable pods, but it looks at
what they request together (CPU, memory, GPUs, zone, architecture, taints) and
asks EC2 directly for an instance that fits them. The constraints live in a
`NodePool` as requirements such as "families c, m, r, generation above 4", and
Karpenter picks from everything that matches. There are no node groups to size or
keep in step with the workload.

| | Cluster Autoscaler | Karpenter |
| :--- | :--- | :--- |
| Unit it scales | Auto Scaling group | Individual node, launched through EC2 Fleet |
| Instance choice | Fixed per group, decided in advance | Chosen per batch of pending pods from the NodePool's requirements |
| Adding a new instance family | New node group | Edit one requirement |
| Spot diversity | Mixed instances policy per group | Many instance types and zones considered on every launch |
| Removing nodes | Scale-down of underused nodes | Consolidation: delete empty nodes, and replace or merge underused ones |
| Node replacement (AMI, expiry) | Roll the group yourself | Drift detection and `expireAfter` |
| Works outside AWS | Yes, many providers | AWS first, with other providers in progress |

**Why Karpenter fits this workload.** This cluster has two kinds of demand that
look nothing alike: ordinary CPU services, and GPU pods that each need exactly
one GPU, with a large image and weights to load. With Cluster Autoscaler that is
at least one node group per GPU instance type we might want, each sized
separately, and a spot interruption in one group does not move the load to
another family. With Karpenter the GPU pool is one object that allows g5 and g6
in two sizes, and a GPU pod arriving is enough to make a node appear.

**What it costs.**
- Karpenter is AWS-specific in practice. A team that runs on several clouds gets
  one autoscaler everywhere from Cluster Autoscaler, and two tools with
  Karpenter.
- It needs its own permissions, an SQS queue for interruption notices, and a
  place to run that is not a Karpenter-managed node (see decision 7).
- Consolidation moves pods. That is good for cost and bad for anything slow to
  start, which is why the GPU pool sets it differently (decision 3).
- More freedom in instance choice means less predictable capacity per pod. The
  `limits` on each pool are what keep that bounded.

**When Cluster Autoscaler is still the right call.** A cluster that is not on
AWS. A fleet of identical nodes where the groups are already well sized and
change rarely. A team that must keep one autoscaler across providers. A
compliance setup that pins the exact set of node shapes in advance and treats a
new instance type as a change to review.

## 2. Spot first, on-demand as the fallback

Both pools allow `spot` and `on-demand`. When both are allowed, Karpenter prefers
spot because it is cheaper, and falls back to on-demand if spot has no capacity
for the request.

Two things make that safe enough:
- **Wide choice of instance types.** Spot interruptions are per capacity pool
  (instance type in a zone). The CPU pool allows three categories across many
  generations, so Karpenter has many pools to pick from. The GPU pool is narrower
  by necessity, g5 and g6 in two sizes, so GPU spot capacity can run out sooner,
  and the on-demand fallback matters more there.
- **Interruption handling.** The SQS queue delivers the two-minute spot notice and
  rebalance recommendations to Karpenter, which starts a replacement node and
  drains the old one before it disappears.

What spot does to each workload:
- The serving Deployment tolerates it, because a replica can restart and the
  Service sends traffic to the others. With the default `minReplicaCount: 1`,
  one interruption means a gap while a new GPU node and the model load. Run
  `minReplicaCount: 2` if that gap is not acceptable.
- The training Job restarts from the beginning, since it keeps no checkpoint.
  That is fine for a run of this size and wrong for a long one. The fix for a long
  run is to checkpoint to S3 and resume, or to run it on-demand.

## 3. Consolidation is different for CPU and GPU

The CPU pool uses `WhenEmptyOrUnderutilized` with `consolidateAfter: 1m`.
Karpenter removes empty nodes and also repacks pods onto fewer or cheaper nodes.
CPU pods restart in seconds, so repacking is cheap.

The GPU pool uses `WhenEmpty` with `consolidateAfter: 5m`. Moving a pod off a GPU
node means a new node, a pulled image and reloaded weights, which takes minutes
and drops requests in the meantime. So the pool only deletes a node that has
nothing running on it. The cost is that a half-used GPU node is not repacked, so
GPU spend can sit above the minimum. That is a deliberate trade of cost for
availability, and the thing to measure is how often GPU nodes sit under half used.

The `budgets` limit how many nodes can be disrupted at once: 10% on CPU, one node
at a time on GPU.

## 4. Graviton (arm64) next to x86, with taints, tolerations and affinity

There are three NodePools, and each is exactly one architecture:

| Pool | Architecture | Runs |
| :--- | :--- | :--- |
| `cpu` | amd64 | General CPU workloads that have no reason to be on arm64 |
| `graviton` | arm64 | CPU work whose images are multi-arch. Here, the eval runner |
| `gpu` | amd64 | vLLM serving and LoRA training |

A test (`tests/test_scheduling.py`) fails if a pool lists more than one
architecture, which is how a mixed pool would produce nodes nobody planned for.

**Why the GPU work stays on x86.** The images decide it. I checked each with
`docker manifest inspect`:

| Image | Architectures published |
| :--- | :--- |
| `vllm/vllm-openai:v0.6.6` | amd64 |
| `pytorch/pytorch:2.4.1-cuda12.1-cudnn9-runtime` (training base) | amd64 |
| `python:3.12-slim`, `amazon/aws-cli:2.17.0` | amd64 and arm64 (Python also others) |
| KEDA 2.20.2, Prometheus v3.15.0, Karpenter controller 1.3.3 | amd64 and arm64 |

A pod whose image has no arm64 build crashes with `exec format error` on a
Graviton node. AWS also sells Graviton instances with a GPU (the g5g family, with
a T4G). I did not evaluate them: the serving and training images would need
arm64 CUDA builds of the whole stack first.

**Why the eval runner is on Graviton.** It is a plain Python script that makes
HTTP calls to the model, so it needs no GPU and no x86 library, and both of its
images have arm64 builds. Graviton instances are generally priced lower than
comparable x86 ones per hour, but whether that is cheaper for this job is a
measurement I have not made, so no saving is claimed.

**Taints, tolerations and affinity do different jobs, and the pattern needs all
three.**

| Mechanism | Who states it | What it does |
| :--- | :--- | :--- |
| Taint | The node (set by the NodePool) | Repels every pod that does not tolerate it |
| Toleration | The pod | Allows the pod onto a tainted node. It does not pull the pod there |
| Node affinity | The pod | Says which nodes the pod wants, by label such as `kubernetes.io/arch` |

A toleration alone only permits; the scheduler may still put the pod on an
ordinary x86 node. Affinity alone does not keep other pods off the node. So:

- **Graviton pool:** the taint `arch=arm64:NoSchedule` keeps every pod that has
  not opted in off arm64, so a pod with an x86-only image cannot land there by
  accident. The eval Job tolerates the taint and also requires
  `kubernetes.io/arch In [arm64]`.
- **GPU pool:** the taint `nvidia.com/gpu` keeps CPU pods from occupying GPU
  nodes. The vLLM and training pods tolerate it and require
  `kubernetes.io/arch In [amd64]` and `karpenter.sh/nodepool In [gpu]`.
- **Default:** a pod that says nothing about architecture goes to the untainted
  `cpu` pool, which is amd64. Graviton is opt-in, so the failure mode of a
  forgotten setting is "ran on x86", not "crashed on arm".

**Required or preferred.** Architecture is `required...`, because the wrong one
means a crash, not a slower pod. The spot preference on the eval Job and the
spread of serving replicas across nodes (`podAntiAffinity`) are
`preferred...`, because they are worth having but should not leave a pod Pending
when they cannot be met.

**How Karpenter uses these.** A pending pod's tolerations, node affinity and
resource requests are what Karpenter reads to decide which pool, and which
instance in it, can host the pod. The pod above that tolerates `arch=arm64` and
requires arm64 matches only the Graviton pool, so Karpenter launches an arm64
instance for it. The same `EC2NodeClass` serves both architectures because the
`al2023@latest` alias picks the matching AMI for the instance type.

**What this does not cover.** The always-on system node group is x86. Moving it
to Graviton looks possible since KEDA, Prometheus and the Karpenter controller
all publish arm64 images, but untainted system nodes would then accept any pod
without an architecture set, and one amd64-only add-on would crash. It stays
x86 until each add-on has been checked. Images you build yourself need a
multi-arch build (`docker buildx build --platform linux/amd64,linux/arm64`) to
run on both.

## 5. Autoscaling the model server on load, not CPU

Pod scaling and node scaling are separate loops, and they need to agree:

1. Load rises, so requests queue in vLLM.
2. KEDA reads `vllm:num_requests_running + vllm:num_requests_waiting` from
   Prometheus and raises the Deployment's replica count.
3. The new replica asks for one GPU and cannot be scheduled.
4. Karpenter sees the pending pod and launches a GPU node.
5. The device plugin advertises the GPU, the pod starts, and vLLM loads the model.
6. When load falls, the Horizontal Pod Autoscaler that KEDA manages lowers
   replicas after its scale-down window (300 seconds by default), the node
   empties, and Karpenter removes it after `consolidateAfter`.

**Why not CPU.** The default HPA metric is CPU. A GPU inference server can be at
its limit with the CPU nearly idle, so CPU would scale too late or never. The
number of requests in flight is closer to what users feel.

**Why KEDA and not a plain HPA on a custom metric.** An HPA needs a metrics
adapter to read Prometheus. KEDA does that directly and also supports scale to
zero. The cost is one more controller to run and upgrade.

**What is not tuned.** The threshold of 8 requests per replica is a placeholder.
The right value comes from a load test: find the request count at which latency
starts to climb, then set the threshold below it. Scale-up is slow by nature,
because a new replica needs a node (minutes if no GPU node exists) plus model
load, so the threshold should leave headroom for that delay or the queue grows
while the replica starts.

**Scale to zero.** KEDA could set `minReplicaCount: 0`, and Karpenter would then
remove the GPU node when idle. It is off here because the first request after an
idle period would wait for a node and the model to load.

## 6. Guardrails against runaway cost

- `limits` on each NodePool cap total capacity: 64 CPUs and 256 Gi of memory on
  CPU, four GPUs on GPU. A loop that creates pods stops at the limit instead of
  at the account's quota.
- The GPU pool carries a taint, so only pods that ask for it land there.
- `expireAfter: 720h` replaces every node within 30 days, which keeps nodes
  patched and stops a forgotten node living forever.
- The ceiling of three serving replicas plus one training Job fits inside the four
  GPUs. The numbers in the pool and the ScaledObject are meant to be read together.

## 7. Where Karpenter itself runs

On a small managed node group of two `m6i.large` nodes. Karpenter cannot create
the node that it runs on, so its controller, CoreDNS and the add-ons need capacity
that does not depend on it. This is also the one part of the cluster that
Cluster Autoscaler or a fixed group still handles. The cost is two always-on
nodes.

## 8. Smaller choices

- **Pod Identity instead of IRSA.** Both give pods AWS permissions without
  stored keys. Pod Identity needs no OIDC provider and no trust policy edit per
  cluster, and the association is a plain EKS resource. IRSA is still the choice
  for clusters on versions or platforms where Pod Identity is not available.
- **LoRA instead of full fine-tuning.** The adapter is a small file, trains on one
  24 GB GPU, and can be served next to the base model. Full fine-tuning would need
  more memory and would produce a whole new model to store and serve.
- **One vLLM server answers both models.** `--enable-lora` lets the base weights
  and the adapter share the GPU, so evaluating them is two requests to one
  endpoint, and the comparison is not skewed by hardware differences.
- **Gemma 2 2B.** It fits one A10G or L4 with room for the cache. A larger model
  would need a larger GPU or more than one, which changes the NodePool.
- **A single NAT gateway.** Cheaper, and an availability risk. A production
  cluster would use one per zone.

## 9. What I would monitor

- Pending pod age for GPU pods, which is the time a user waits for scale-up.
- Karpenter's node launch and interruption events, and how often GPU requests
  fall back from spot to on-demand.
- GPU utilisation and GPU node count over time, to see whether `WhenEmpty`
  leaves nodes under half used.
- vLLM queue depth and time to first token, to set the scaling threshold from
  data.
- Spend per workload, using the `karpenter.sh/nodepool` label.

## 10. Known gaps

- Not deployed. The configuration passes `terraform validate` and schema checks
  for every manifest, and the Python is unit tested. Nothing has run on AWS or a
  GPU, so image tags, model fit, startup time and the scaling threshold are
  untested.
- The eval metric is word overlap, a weak proxy for answer quality.
- Training loss covers the prompt as well as the answer.
- No checkpointing, so a spot interruption restarts training.
- Prometheus has no persistent storage, so metric history is lost when it restarts.
- The cluster API endpoint is public. A real deployment would restrict it.
- Which images have an arm64 build was checked once, for the versions pinned
  here. Repeat `docker manifest inspect` when you change a version.
