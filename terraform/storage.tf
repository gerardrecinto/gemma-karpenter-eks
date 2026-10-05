# Where trained adapters and the held-out prompts go, and the identity the
# training Job and the serving pod use to read and write them.

resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.cluster_name}-ml-artifacts-${data.aws_caller_identity.current.account_id}"

  # Lets `terraform destroy` finish. The contents are reproducible by re-running training.
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "aws_iam_policy_document" "pods_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "artifacts_access" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }
}

resource "aws_iam_role" "ml_workload" {
  name               = "${var.cluster_name}-ml-workload"
  assume_role_policy = data.aws_iam_policy_document.pods_assume.json
}

resource "aws_iam_role_policy" "ml_workload" {
  name   = "artifacts-access"
  role   = aws_iam_role.ml_workload.id
  policy = data.aws_iam_policy_document.artifacts_access.json
}

# Binds the ml-workload service account in the ml namespace to that role.
resource "aws_eks_pod_identity_association" "ml_workload" {
  cluster_name    = module.eks.cluster_name
  namespace       = "ml"
  service_account = "ml-workload"
  role_arn        = aws_iam_role.ml_workload.arn
}

resource "aws_ecr_repository" "trainer" {
  name         = "${var.cluster_name}-trainer"
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }
}
