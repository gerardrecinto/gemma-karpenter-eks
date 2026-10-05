variable "region" {
  description = "AWS region. Pick one where you have g5 or g6 GPU quota."
  type        = string
  default     = "us-west-2"
}

variable "cluster_name" {
  description = "EKS cluster name. Also used to tag subnets and security groups for Karpenter discovery."
  type        = string
  default     = "gemma-karpenter"
}

variable "cluster_version" {
  description = "Kubernetes version for the control plane."
  type        = string
  default     = "1.31"
}

variable "karpenter_version" {
  description = "Karpenter Helm chart version."
  type        = string
  default     = "1.3.3"
}

variable "keda_version" {
  description = "KEDA Helm chart version."
  type        = string
  default     = "2.20.2"
}

variable "prometheus_version" {
  description = "Prometheus Helm chart version."
  type        = string
  default     = "29.34.0"
}

variable "nvidia_device_plugin_version" {
  description = "NVIDIA device plugin Helm chart version."
  type        = string
  default     = "0.20.1"
}

variable "tags" {
  description = "Tags applied to everything Terraform creates."
  type        = map(string)
  default = {
    project = "gemma-karpenter-eks"
  }
}
