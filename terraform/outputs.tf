output "cluster_name" {
  value = module.eks.cluster_name
}

output "region" {
  value = var.region
}

output "karpenter_node_role_name" {
  description = "IAM role name the EC2NodeClass gives to the nodes Karpenter launches."
  value       = module.karpenter.node_iam_role_name
}

output "artifacts_bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "trainer_image_repository" {
  value = aws_ecr_repository.trainer.repository_url
}

output "kubeconfig_command" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}
