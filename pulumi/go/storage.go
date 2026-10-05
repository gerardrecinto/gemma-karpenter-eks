package main

import (
	"encoding/json"
	"fmt"

	"github.com/pulumi/pulumi-aws/sdk/v7/go/aws"
	"github.com/pulumi/pulumi-aws/sdk/v7/go/aws/ecr"
	"github.com/pulumi/pulumi-aws/sdk/v7/go/aws/eks"
	"github.com/pulumi/pulumi-aws/sdk/v7/go/aws/iam"
	"github.com/pulumi/pulumi-aws/sdk/v7/go/aws/s3"
	"github.com/pulumi/pulumi/sdk/v3/go/pulumi"
)

// Storage is the storage layer from terraform/storage.tf: where trained adapters
// and held-out prompts go, the identity the training Job and the serving pod use
// to read and write them, and the trainer image repository.
type Storage struct {
	Bucket            *s3.Bucket
	Versioning        *s3.BucketVersioning
	PublicAccessBlock *s3.BucketPublicAccessBlock
	Encryption        *s3.BucketServerSideEncryptionConfiguration
	Role              *iam.Role
	RolePolicy        *iam.RolePolicy
	PodIdentity       *eks.PodIdentityAssociation
	TrainerRepo       *ecr.Repository
}

func mustJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return string(b)
}

func createStorage(ctx *pulumi.Context, clusterName string) (*Storage, error) {
	identity, err := aws.GetCallerIdentity(ctx, nil, nil)
	if err != nil {
		return nil, err
	}

	// ForceDestroy lets `pulumi destroy` finish. The contents are reproducible by re-running training.
	bucket, err := s3.NewBucket(ctx, "artifacts", &s3.BucketArgs{
		Bucket:       pulumi.String(fmt.Sprintf("%s-ml-artifacts-%s", clusterName, identity.AccountId)),
		ForceDestroy: pulumi.Bool(true),
	})
	if err != nil {
		return nil, err
	}
	versioning, err := s3.NewBucketVersioning(ctx, "artifacts", &s3.BucketVersioningArgs{
		Bucket: bucket.ID(),
		VersioningConfiguration: &s3.BucketVersioningVersioningConfigurationArgs{
			Status: pulumi.String("Enabled"),
		},
	})
	if err != nil {
		return nil, err
	}
	pab, err := s3.NewBucketPublicAccessBlock(ctx, "artifacts", &s3.BucketPublicAccessBlockArgs{
		Bucket:                bucket.ID(),
		BlockPublicAcls:       pulumi.Bool(true),
		BlockPublicPolicy:     pulumi.Bool(true),
		IgnorePublicAcls:      pulumi.Bool(true),
		RestrictPublicBuckets: pulumi.Bool(true),
	})
	if err != nil {
		return nil, err
	}
	encryption, err := s3.NewBucketServerSideEncryptionConfiguration(ctx, "artifacts", &s3.BucketServerSideEncryptionConfigurationArgs{
		Bucket: bucket.ID(),
		Rules: s3.BucketServerSideEncryptionConfigurationRuleArray{
			&s3.BucketServerSideEncryptionConfigurationRuleArgs{
				ApplyServerSideEncryptionByDefault: &s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs{
					SseAlgorithm: pulumi.String("AES256"),
				},
			},
		},
	})
	if err != nil {
		return nil, err
	}

	role, err := iam.NewRole(ctx, "ml-workload", &iam.RoleArgs{
		Name: pulumi.String(clusterName + "-ml-workload"),
		AssumeRolePolicy: pulumi.String(mustJSON(map[string]any{
			"Version": "2012-10-17",
			"Statement": []map[string]any{{
				"Effect":    "Allow",
				"Action":    []string{"sts:AssumeRole", "sts:TagSession"},
				"Principal": map[string]string{"Service": "pods.eks.amazonaws.com"},
			}},
		})),
	})
	if err != nil {
		return nil, err
	}
	rolePolicy, err := iam.NewRolePolicy(ctx, "ml-workload", &iam.RolePolicyArgs{
		Name: pulumi.String("artifacts-access"),
		Role: role.ID(),
		Policy: bucket.Arn.ApplyT(func(arn string) string {
			return mustJSON(map[string]any{
				"Version": "2012-10-17",
				"Statement": []map[string]any{
					{"Effect": "Allow", "Action": []string{"s3:ListBucket"}, "Resource": arn},
					{"Effect": "Allow", "Action": []string{"s3:GetObject", "s3:PutObject"}, "Resource": arn + "/*"},
				},
			})
		}).(pulumi.StringOutput),
	})
	if err != nil {
		return nil, err
	}

	// Binds the ml-workload service account in the ml namespace to that role.
	podIdentity, err := eks.NewPodIdentityAssociation(ctx, "ml-workload", &eks.PodIdentityAssociationArgs{
		ClusterName:    pulumi.String(clusterName),
		Namespace:      pulumi.String("ml"),
		ServiceAccount: pulumi.String("ml-workload"),
		RoleArn:        role.Arn,
	})
	if err != nil {
		return nil, err
	}

	repo, err := ecr.NewRepository(ctx, "trainer", &ecr.RepositoryArgs{
		Name:        pulumi.String(clusterName + "-trainer"),
		ForceDelete: pulumi.Bool(true),
		ImageScanningConfiguration: &ecr.RepositoryImageScanningConfigurationArgs{
			ScanOnPush: pulumi.Bool(true),
		},
	})
	if err != nil {
		return nil, err
	}

	return &Storage{bucket, versioning, pab, encryption, role, rolePolicy, podIdentity, repo}, nil
}
