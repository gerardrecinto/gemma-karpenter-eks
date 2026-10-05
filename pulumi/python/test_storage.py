"""Unit tests that run the program against Pulumi's mocks, with no AWS account."""

import json
import unittest

import pulumi


class Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        outputs = dict(args.inputs)
        outputs["arn"] = f"arn:aws:mock:::{args.name}"
        if args.typ == "aws:ecr/repository:Repository":
            outputs["repositoryUrl"] = f"123456789012.dkr.ecr.us-west-2.amazonaws.com/{args.inputs['name']}"
        return [f"{args.name}_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        if args.token == "aws:index/getCallerIdentity:getCallerIdentity":
            return {"accountId": "123456789012", "arn": "arn:aws:iam::123456789012:user/test", "userId": "test"}
        return {}


pulumi.runtime.set_mocks(Mocks(), project="gemma-karpenter-storage", stack="test", preview=False)

import storage  # noqa: E402  (must come after set_mocks)

infra = storage.create_storage("gemma-karpenter")


def run(outputs, check):
    return pulumi.Output.all(*outputs).apply(lambda values: check(*values))


class StorageTests(unittest.TestCase):
    @pulumi.runtime.test
    def test_bucket_name_includes_cluster_and_account(self):
        def check(name):
            self.assertEqual(name, "gemma-karpenter-ml-artifacts-123456789012")

        return run([infra.bucket.bucket], check)

    @pulumi.runtime.test
    def test_versioning_is_enabled(self):
        def check(cfg):
            self.assertEqual(cfg["status"], "Enabled")

        return run([infra.versioning.versioning_configuration], check)

    @pulumi.runtime.test
    def test_every_public_access_block_is_on(self):
        def check(a, b, c, d):
            self.assertTrue(all([a, b, c, d]))

        pab = infra.public_access_block
        return run(
            [pab.block_public_acls, pab.block_public_policy, pab.ignore_public_acls, pab.restrict_public_buckets],
            check,
        )

    @pulumi.runtime.test
    def test_encryption_is_aes256(self):
        def check(rules):
            self.assertEqual(rules[0]["apply_server_side_encryption_by_default"]["sse_algorithm"], "AES256")

        return run([infra.encryption.rules], check)

    @pulumi.runtime.test
    def test_only_the_pod_identity_service_can_assume_the_role(self):
        def check(policy):
            doc = json.loads(policy)
            principals = [s["Principal"]["Service"] for s in doc["Statement"]]
            self.assertEqual(principals, ["pods.eks.amazonaws.com"])

        return run([infra.role.assume_role_policy], check)

    @pulumi.runtime.test
    def test_role_can_only_read_and_write_objects_in_the_bucket(self):
        def check(policy):
            doc = json.loads(policy)
            actions = sorted(a for s in doc["Statement"] for a in s["Action"])
            self.assertEqual(actions, ["s3:GetObject", "s3:ListBucket", "s3:PutObject"])
            self.assertNotIn("*", [s["Resource"] for s in doc["Statement"]])

        return run([infra.role_policy.policy], check)

    @pulumi.runtime.test
    def test_pod_identity_binds_the_ml_workload_service_account(self):
        def check(ns, sa):
            self.assertEqual((ns, sa), ("ml", "ml-workload"))

        return run([infra.pod_identity.namespace, infra.pod_identity.service_account], check)

    @pulumi.runtime.test
    def test_trainer_repository_scans_on_push(self):
        def check(cfg):
            self.assertTrue(cfg["scan_on_push"])

        return run([infra.trainer_repo.image_scanning_configuration], check)


if __name__ == "__main__":
    unittest.main()
