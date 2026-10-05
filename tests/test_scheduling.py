"""Scheduling rules the cluster depends on, checked against the manifests.

A pod that asks for a GPU has to land on an amd64 node from the GPU pool. A pod
that opts in to Graviton has to tolerate its taint and require arm64. These are
the mistakes that only show up as a Pending pod or an "exec format error" on a
real cluster, so they are checked here instead.
"""

import glob
import os

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")


def load(pattern):
    docs = []
    for path in sorted(glob.glob(os.path.join(ROOT, pattern))):
        with open(path) as f:
            for d in yaml.safe_load_all(f):
                if d:
                    docs.append((os.path.relpath(path, ROOT), d))
    return docs


def pod_specs():
    out = []
    for pattern in ("k8s/serving/base/deployment.yaml", "k8s/training/job.yaml", "k8s/eval/job.yaml"):
        for path, d in load(pattern):
            out.append((f"{path}:{d['metadata']['name']}", d["spec"]["template"]["spec"]))
    return out


def nodepools():
    return {d["metadata"]["name"]: d for _, d in load("k8s/karpenter/nodepool-*.yaml")}


def wants_gpu(spec):
    return any(
        "nvidia.com/gpu" in (c.get("resources", {}).get("requests", {}))
        for c in spec["containers"]
    )


def required_terms(spec):
    na = spec.get("affinity", {}).get("nodeAffinity", {})
    req = na.get("requiredDuringSchedulingIgnoredDuringExecution", {})
    exprs = []
    for term in req.get("nodeSelectorTerms", []):
        exprs.extend(term.get("matchExpressions", []))
    return exprs


def required_values(spec, key):
    return [v for e in required_terms(spec) if e["key"] == key and e["operator"] == "In" for v in e["values"]]


def tolerates(spec, key, value=None):
    for t in spec.get("tolerations", []):
        if t.get("key") == key and (value is None or t.get("value") == value or t.get("operator") == "Exists"):
            return True
    return False


def test_there_are_pod_specs_to_check():
    # Guards against the glob patterns going stale and the tests passing on nothing.
    assert len(pod_specs()) == 3


@pytest.mark.parametrize("name,spec", pod_specs())
def test_gpu_pods_require_amd64_and_the_gpu_pool(name, spec):
    if not wants_gpu(spec):
        pytest.skip("does not request a GPU")
    assert required_values(spec, "kubernetes.io/arch") == ["amd64"], f"{name} must require amd64"
    assert required_values(spec, "karpenter.sh/nodepool") == ["gpu"], f"{name} must require the gpu pool"
    assert tolerates(spec, "nvidia.com/gpu"), f"{name} must tolerate the GPU taint"
    assert not tolerates(spec, "arch"), f"{name} must not tolerate the Graviton taint"


@pytest.mark.parametrize("name,spec", pod_specs())
def test_pods_on_graviton_tolerate_the_taint_and_require_arm64(name, spec):
    if not tolerates(spec, "arch", "arm64"):
        pytest.skip("does not opt in to Graviton")
    assert required_values(spec, "kubernetes.io/arch") == ["arm64"], f"{name} tolerates arm64 but does not require it"
    assert not wants_gpu(spec), f"{name} is on Graviton but asks for a GPU"


@pytest.mark.parametrize("name,spec", pod_specs())
def test_a_pod_that_requires_arm64_can_actually_schedule(name, spec):
    # Requiring arm64 without tolerating the taint leaves the pod Pending forever.
    if required_values(spec, "kubernetes.io/arch") == ["arm64"]:
        assert tolerates(spec, "arch", "arm64"), f"{name} requires arm64 but cannot tolerate the arm64 taint"


def test_each_pool_is_one_architecture():
    arches = {}
    for name, pool in nodepools().items():
        reqs = pool["spec"]["template"]["spec"]["requirements"]
        arch = [r for r in reqs if r["key"] == "kubernetes.io/arch"]
        assert len(arch) == 1 and arch[0]["operator"] == "In", name
        assert len(arch[0]["values"]) == 1, f"{name} mixes architectures"
        arches[name] = arch[0]["values"][0]
    assert arches == {"cpu": "amd64", "gpu": "amd64", "graviton": "arm64"}


def test_graviton_and_gpu_pools_are_tainted_and_the_cpu_pool_is_not():
    pools = nodepools()
    taints = {n: p["spec"]["template"]["spec"].get("taints", []) for n, p in pools.items()}
    assert taints["cpu"] == []
    assert taints["graviton"] == [{"key": "arch", "value": "arm64", "effect": "NoSchedule"}]
    assert taints["gpu"] == [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}]


def test_every_pool_has_a_limit():
    for name, pool in nodepools().items():
        assert pool["spec"].get("limits"), f"{name} has no limits"


def test_gpu_limit_covers_the_most_gpus_the_workloads_can_ask_for():
    limit = int(nodepools()["gpu"]["spec"]["limits"]["nvidia.com/gpu"])
    scaled = load("k8s/serving/base/scaledobject.yaml")[0][1]["spec"]["maxReplicaCount"]
    # the serving replicas at their maximum, plus one training Job
    assert scaled + 1 <= limit
