"""The storage layer from terraform/storage.tf, written with Pulumi in Python.

Where trained adapters and held-out prompts go, the identity the training Job
and the serving pod use to read and write them, and the trainer image repository.
"""

import json
from dataclasses import dataclass

import pulumi
import pulumi_aws as aws


@dataclass
class Storage:
    bucket: aws.s3.Bucket
    versioning: aws.s3.BucketVersioning
    public_access_block: aws.s3.BucketPublicAccessBlock
    encryption: aws.s3.BucketServerSideEncryptionConfiguration
    role: aws.iam.Role
    role_policy: aws.iam.RolePolicy
    pod_identity: aws.eks.PodIdentityAssociation
    trainer_repo: aws.ecr.Repository


def create_storage(cluster_name: str) -> Storage:
    account_id = aws.get_caller_identity().account_id

    # force_destroy lets `pulumi destroy` finish. The contents are reproducible by re-running training.
    bucket = aws.s3.Bucket(
        "artifacts",
        bucket=f"{cluster_name}-ml-artifacts-{account_id}",
        force_destroy=True,
    )
    versioning = aws.s3.BucketVersioning(
        "artifacts",
        bucket=bucket.id,
        versioning_configuration={"status": "Enabled"},
    )
    public_access_block = aws.s3.BucketPublicAccessBlock(
        "artifacts",
        bucket=bucket.id,
        block_public_acls=True,
        block_public_policy=True,
        ignore_public_acls=True,
        restrict_public_buckets=True,
    )
    encryption = aws.s3.BucketServerSideEncryptionConfiguration(
        "artifacts",
        bucket=bucket.id,
        rules=[{"apply_server_side_encryption_by_default": {"sse_algorithm": "AES256"}}],
    )

    role = aws.iam.Role(
        "ml-workload",
        name=f"{cluster_name}-ml-workload",
        assume_role_policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["sts:AssumeRole", "sts:TagSession"],
                        "Principal": {"Service": "pods.eks.amazonaws.com"},
                    }
                ],
            }
        ),
    )
    role_policy = aws.iam.RolePolicy(
        "ml-workload",
        name="artifacts-access",
        role=role.id,
        policy=bucket.arn.apply(
            lambda arn: json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": arn},
                        {
                            "Effect": "Allow",
                            "Action": ["s3:GetObject", "s3:PutObject"],
                            "Resource": f"{arn}/*",
                        },
                    ],
                }
            )
        ),
    )

    # Binds the ml-workload service account in the ml namespace to that role.
    pod_identity = aws.eks.PodIdentityAssociation(
        "ml-workload",
        cluster_name=cluster_name,
        namespace="ml",
        service_account="ml-workload",
        role_arn=role.arn,
    )

    trainer_repo = aws.ecr.Repository(
        "trainer",
        name=f"{cluster_name}-trainer",
        force_delete=True,
        image_scanning_configuration={"scan_on_push": True},
    )

    return Storage(
        bucket, versioning, public_access_block, encryption, role, role_policy, pod_identity, trainer_repo
    )
