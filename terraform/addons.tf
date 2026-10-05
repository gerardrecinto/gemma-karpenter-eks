# The three pieces that turn "a GPU node exists" into "the model scales on load":
# the device plugin advertises nvidia.com/gpu, Prometheus keeps the vLLM
# metrics, and KEDA turns one of those metrics into a replica count.

resource "helm_release" "nvidia_device_plugin" {
  name       = "nvidia-device-plugin"
  namespace  = "kube-system"
  repository = "https://nvidia.github.io/k8s-device-plugin"
  chart      = "nvidia-device-plugin"
  version    = var.nvidia_device_plugin_version

  # Run only on nodes the GPU NodePool creates. The chart's default affinity
  # relies on node feature discovery, which this cluster does not install.
  values = [
    yamlencode({
      affinity     = null
      nodeSelector = { "karpenter.sh/nodepool" = "gpu" }
      tolerations = [
        { key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }
      ]
    })
  ]

  depends_on = [helm_release.karpenter]
}

resource "helm_release" "prometheus" {
  name             = "prometheus"
  namespace        = "monitoring"
  create_namespace = true
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "prometheus"
  version          = var.prometheus_version

  # Only the server. The default scrape config finds pods by the
  # prometheus.io/scrape annotation, which the vLLM Deployment sets.
  values = [
    yamlencode({
      alertmanager             = { enabled = false }
      prometheus-pushgateway   = { enabled = false }
      prometheus-node-exporter = { enabled = false }
      kube-state-metrics       = { enabled = false }
      server                   = { persistentVolume = { enabled = false } }
    })
  ]

  depends_on = [module.eks]
}

resource "helm_release" "keda" {
  name             = "keda"
  namespace        = "keda"
  create_namespace = true
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  version          = var.keda_version

  depends_on = [module.eks]
}
