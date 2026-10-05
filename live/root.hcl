# Root configuration: remote state and the Terraform source, written once.
# Set TG_STATE_BUCKET and TG_LOCK_TABLE to a bucket and DynamoDB table you own
# before a real apply. Set TG_LOCAL_STATE=1 to keep state in a local file, which
# is how `make validate` checks this directory without an AWS account.
locals {
  common    = read_terragrunt_config("${get_parent_terragrunt_dir()}/common.hcl")
  use_local = get_env("TG_LOCAL_STATE", "") != ""
}

remote_state {
  backend = local.use_local ? "local" : "s3"
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }
  config = local.use_local ? {
    path = "${get_terragrunt_dir()}/terraform.tfstate"
    } : {
    bucket         = get_env("TG_STATE_BUCKET", "unset-state-bucket")
    key            = "${path_relative_to_include()}/terraform.tfstate"
    region         = local.common.locals.region
    encrypt        = true
    dynamodb_table = get_env("TG_LOCK_TABLE", "unset-lock-table")
  }
}

terraform {
  source = "${get_parent_terragrunt_dir()}/../terraform"
}

inputs = {
  region          = local.common.locals.region
  cluster_version = local.common.locals.cluster_version
}
