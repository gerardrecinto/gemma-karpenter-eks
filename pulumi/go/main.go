package main

import (
	"github.com/pulumi/pulumi/sdk/v3/go/pulumi"
	"github.com/pulumi/pulumi/sdk/v3/go/pulumi/config"
)

func main() {
	pulumi.Run(func(ctx *pulumi.Context) error {
		clusterName := config.Get(ctx, "clusterName")
		if clusterName == "" {
			clusterName = "gemma-karpenter"
		}
		s, err := createStorage(ctx, clusterName)
		if err != nil {
			return err
		}
		ctx.Export("artifacts_bucket", s.Bucket.Bucket)
		ctx.Export("trainer_image_repository", s.TrainerRepo.RepositoryUrl)
		return nil
	})
}
