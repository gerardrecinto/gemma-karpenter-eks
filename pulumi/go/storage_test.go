package main

import (
	"encoding/json"
	"reflect"
	"sort"
	"sync"
	"testing"

	"github.com/pulumi/pulumi/sdk/v3/go/common/resource"
	"github.com/pulumi/pulumi/sdk/v3/go/pulumi"
)

// mocks lets the program run against Pulumi's test engine, with no AWS account.
type mocks int

func (mocks) NewResource(args pulumi.MockResourceArgs) (string, resource.PropertyMap, error) {
	outputs := args.Inputs.Copy()
	outputs["arn"] = resource.NewStringProperty("arn:aws:mock:::" + args.Name)
	if args.TypeToken == "aws:ecr/repository:Repository" {
		outputs["repositoryUrl"] = resource.NewStringProperty("123456789012.dkr.ecr.us-west-2.amazonaws.com/" + args.Inputs["name"].StringValue())
	}
	return args.Name + "_id", outputs, nil
}

func (mocks) Call(args pulumi.MockCallArgs) (resource.PropertyMap, error) {
	if args.Token == "aws:index/getCallerIdentity:getCallerIdentity" {
		return resource.PropertyMap{
			"accountId": resource.NewStringProperty("123456789012"),
			"arn":       resource.NewStringProperty("arn:aws:iam::123456789012:user/test"),
			"userId":    resource.NewStringProperty("test"),
		}, nil
	}
	return args.Args, nil
}

// deref unwraps the pointer some optional outputs resolve to.
func deref(v any) any {
	rv := reflect.ValueOf(v)
	if rv.Kind() == reflect.Ptr {
		if rv.IsNil() {
			return nil
		}
		return rv.Elem().Interface()
	}
	return v
}

// withStorage runs createStorage under the mocks and hands the result to check.
func withStorage(t *testing.T, check func(s *Storage, wait func(pulumi.Output, func(v any)))) {
	t.Helper()
	err := pulumi.RunErr(func(ctx *pulumi.Context) error {
		s, err := createStorage(ctx, "gemma-karpenter")
		if err != nil {
			return err
		}
		var wg sync.WaitGroup
		wait := func(o pulumi.Output, f func(v any)) {
			wg.Add(1)
			o.ApplyT(func(v any) any {
				defer wg.Done()
				f(v)
				return nil
			})
		}
		check(s, wait)
		wg.Wait()
		return nil
	}, pulumi.WithMocks("gemma-karpenter-storage-go", "test", mocks(0)))
	if err != nil {
		t.Fatal(err)
	}
}

func TestBucketNameIncludesClusterAndAccount(t *testing.T) {
	withStorage(t, func(s *Storage, wait func(pulumi.Output, func(any))) {
		wait(s.Bucket.Bucket, func(v any) {
			if got := v.(string); got != "gemma-karpenter-ml-artifacts-123456789012" {
				t.Errorf("bucket name = %q", got)
			}
		})
	})
}

func TestVersioningIsEnabled(t *testing.T) {
	withStorage(t, func(s *Storage, wait func(pulumi.Output, func(any))) {
		wait(s.Versioning.VersioningConfiguration.Status(), func(v any) {
			if got := deref(v); got != "Enabled" {
				t.Errorf("versioning status = %q", got)
			}
		})
	})
}

func TestEveryPublicAccessBlockIsOn(t *testing.T) {
	withStorage(t, func(s *Storage, wait func(pulumi.Output, func(any))) {
		p := s.PublicAccessBlock
		for name, o := range map[string]pulumi.Output{
			"block_public_acls":       p.BlockPublicAcls,
			"block_public_policy":     p.BlockPublicPolicy,
			"ignore_public_acls":      p.IgnorePublicAcls,
			"restrict_public_buckets": p.RestrictPublicBuckets,
		} {
			name := name
			wait(o, func(v any) {
				if b, ok := deref(v).(bool); !ok || !b {
					t.Errorf("%s is not true: %v", name, deref(v))
				}
			})
		}
	})
}

func TestEncryptionIsAES256(t *testing.T) {
	withStorage(t, func(s *Storage, wait func(pulumi.Output, func(any))) {
		wait(s.Encryption.Rules.Index(pulumi.Int(0)).ApplyServerSideEncryptionByDefault().SseAlgorithm(), func(v any) {
			if got := deref(v); got != "AES256" {
				t.Errorf("sse algorithm = %q", got)
			}
		})
	})
}

func TestOnlyThePodIdentityServiceCanAssumeTheRole(t *testing.T) {
	withStorage(t, func(s *Storage, wait func(pulumi.Output, func(any))) {
		wait(s.Role.AssumeRolePolicy, func(v any) {
			var doc struct {
				Statement []struct{ Principal struct{ Service string } }
			}
			if err := json.Unmarshal([]byte(v.(string)), &doc); err != nil {
				t.Fatal(err)
			}
			if len(doc.Statement) != 1 || doc.Statement[0].Principal.Service != "pods.eks.amazonaws.com" {
				t.Errorf("unexpected trust policy: %v", v)
			}
		})
	})
}

func TestRoleCanOnlyReadAndWriteObjectsInTheBucket(t *testing.T) {
	withStorage(t, func(s *Storage, wait func(pulumi.Output, func(any))) {
		wait(s.RolePolicy.Policy, func(v any) {
			var doc struct {
				Statement []struct {
					Action   []string
					Resource string
				}
			}
			if err := json.Unmarshal([]byte(v.(string)), &doc); err != nil {
				t.Fatal(err)
			}
			var actions []string
			for _, st := range doc.Statement {
				actions = append(actions, st.Action...)
				if st.Resource == "*" {
					t.Errorf("policy grants a wildcard resource")
				}
			}
			sort.Strings(actions)
			want := []string{"s3:GetObject", "s3:ListBucket", "s3:PutObject"}
			if len(actions) != 3 || actions[0] != want[0] || actions[1] != want[1] || actions[2] != want[2] {
				t.Errorf("actions = %v", actions)
			}
		})
	})
}

func TestPodIdentityBindsTheMlWorkloadServiceAccount(t *testing.T) {
	withStorage(t, func(s *Storage, wait func(pulumi.Output, func(any))) {
		wait(s.PodIdentity.Namespace, func(v any) {
			if deref(v) != "ml" {
				t.Errorf("namespace = %v", v)
			}
		})
		wait(s.PodIdentity.ServiceAccount, func(v any) {
			if deref(v) != "ml-workload" {
				t.Errorf("service account = %v", v)
			}
		})
	})
}

func TestTrainerRepositoryScansOnPush(t *testing.T) {
	withStorage(t, func(s *Storage, wait func(pulumi.Output, func(any))) {
		wait(s.TrainerRepo.ImageScanningConfiguration.ScanOnPush(), func(v any) {
			if b, ok := deref(v).(bool); !ok || !b {
				t.Errorf("scan on push = %v", deref(v))
			}
		})
	})
}
