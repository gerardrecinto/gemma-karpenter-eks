import pulumi

from storage import create_storage

cluster_name = pulumi.Config().get("clusterName") or "gemma-karpenter"
storage = create_storage(cluster_name)

pulumi.export("artifacts_bucket", storage.bucket.bucket)
pulumi.export("trainer_image_repository", storage.trainer_repo.repository_url)
