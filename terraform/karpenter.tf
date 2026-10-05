# IAM for the controller and for the nodes it launches, plus the SQS queue that
# carries spot interruption and rebalance notices to it.
module "karpenter" {
  source  = "terraform-aws-modules/eks/aws//modules/karpenter"
  version = "~> 20.0"

  cluster_name = module.eks.cluster_name

  enable_v1_permissions = true

  # EKS Pod Identity, not IRSA, gives the controller its AWS permissions.
  create_pod_identity_association = true
  namespace                       = "kube-system"

  node_iam_role_use_name_prefix = false
  node_iam_role_name            = "${var.cluster_name}-karpenter-node"
  node_iam_role_additional_policies = {
    AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
  }
}

resource "helm_release" "karpenter" {
  name       = "karpenter"
  namespace  = "kube-system"
  repository = "oci://public.ecr.aws/karpenter"
  chart      = "karpenter"
  version    = var.karpenter_version
  wait       = true

  values = [
    yamlencode({
      settings = {
        clusterName       = module.eks.cluster_name
        interruptionQueue = module.karpenter.queue_name
      }
      controller = {
        resources = {
          requests = { cpu = "1", memory = "1Gi" }
          limits   = { cpu = "1", memory = "1Gi" }
        }
      }
    })
  ]

  depends_on = [module.eks]
}
