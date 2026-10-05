# The control plane plus a small managed node group. The managed group runs
# Karpenter itself and the cluster add-ons. Karpenter cannot provision the
# node it runs on, so the controller needs capacity that does not depend on it.
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = var.cluster_name
  cluster_version = var.cluster_version

  cluster_endpoint_public_access           = true
  enable_cluster_creator_admin_permissions = true

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  cluster_addons = {
    coredns                = {}
    kube-proxy             = {}
    vpc-cni                = {}
    eks-pod-identity-agent = {}
  }

  eks_managed_node_groups = {
    system = {
      instance_types = ["m6i.large"]
      ami_type       = "AL2023_x86_64_STANDARD"

      min_size     = 2
      max_size     = 3
      desired_size = 2

      labels = {
        workload = "system"
      }
    }
  }

  # Karpenter launches nodes into the cluster's node security group, found by this tag.
  node_security_group_tags = {
    "karpenter.sh/discovery" = var.cluster_name
  }
}
