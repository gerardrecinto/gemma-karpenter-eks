# Values every environment shares. Each environment's terragrunt.hcl reads this
# file and overrides only what differs, so a change here reaches all of them.
locals {
  region          = "us-west-2"
  cluster_version = "1.31"
  tags = {
    project = "gemma-karpenter-eks"
  }
}
