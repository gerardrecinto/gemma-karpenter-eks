include "root" {
  path = find_in_parent_folders("root.hcl")
}

locals {
  common = read_terragrunt_config("${get_parent_terragrunt_dir()}/common.hcl")
}

inputs = {
  cluster_name = "gemma-karpenter-prod"
  tags         = merge(local.common.locals.tags, { environment = "prod" })
}
