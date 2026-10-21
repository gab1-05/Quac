# =============================================================================
# Quac — input variables (AWS provider v5.x)
# =============================================================================

variable "aws_region" {
  description = "Region hosting every Quac resource (API, worker, data plane)."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project slug used as the root of all resource names/tags."
  type        = string
  default     = "quac"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.project))
    error_message = "project must be 2-21 lowercase alphanumeric/hyphen characters."
  }
}

variable "environment" {
  description = "Deployment environment (dev, staging, prod)."
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "vpc_cidr" {
  description = "CIDR block for the Quac VPC (public + private subnets for Fargate)."
  type        = string
  default     = "10.42.0.0/16"
}

variable "api_certificate_arn" {
  description = "ACM certificate ARN attached to the ALB HTTPS listener; issue with ACM or import an existing certificate."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:acm:.+:certificate/.+$", var.api_certificate_arn))
    error_message = "api_certificate_arn must be an ACM certificate ARN."
  }
}

variable "availability_zones" {
  description = "Two AZs for ALB + Fargate multi-AZ redundancy."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

variable "api_image_tag" {
  description = "ECR image tag for the Quac API container."
  type        = string
  default     = "latest"
}

variable "worker_image_uri" {
  description = "Full ECR URI of the Quac worker Lambda container image (e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com/quac-worker:latest)."
  type        = string
}

variable "worker_architecture" {
  description = "Lambda instruction set architecture; must match the wheel architecture baked into worker/Dockerfile.worker (arm64 -> manylinux2014_aarch64)."
  type        = string
  default     = "arm64"

  validation {
    condition     = contains(["arm64", "x86_64"], var.worker_architecture)
    error_message = "worker_architecture must be arm64 or x86_64."
  }
}

variable "worker_memory_size" {
  description = "Lambda memory (MB). Statevector sims are memory-bound: 2**28 complex128 amplitudes need ~4 GiB; 2048 MB balances cost against the 28-qubit cap."
  type        = number
  default     = 2048

  validation {
    condition     = var.worker_memory_size >= 1024 && var.worker_memory_size <= 10240
    error_message = "worker_memory_size must be between 1024 and 10240 MB."
  }
}

variable "worker_timeout_seconds" {
  description = "Lambda timeout, capped at the service maximum of 600 s; worker.py arms an internal SIGALRM at 570 s (9:30) below this ceiling."
  type        = number
  default     = 600

  validation {
    condition     = var.worker_timeout_seconds >= 30 && var.worker_timeout_seconds <= 600
    error_message = "worker_timeout_seconds must be within [30, 600] (Lambda maximum)."
  }
}

variable "sqs_max_receive_count" {
  description = "Receive attempts before a message is routed to the dead-letter queue."
  type        = number
  default     = 3
}

variable "sqs_visibility_timeout_seconds" {
  description = "SQS visibility timeout; must exceed the Lambda timeout (600 s). 660 s gives a 60 s buffer so in-flight messages are not redelivered mid-run."
  type        = number
  default     = 660
}

variable "sqs_message_retention_seconds" {
  description = "Message retention on the primary queue (14 days maximum)."
  type        = number
  default     = 1209600
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for API and worker log groups."
  type        = number
  default     = 30
}

variable "api_cpu" {
  description = "Fargate vCPU units for each Quac API task (1024 = 1 vCPU)."
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "Fargate memory (MiB) for each Quac API task."
  type        = number
  default     = 1024
}

variable "api_desired_count" {
  description = "Desired task count behind the ALB (multi-AZ minimum is 2)."
  type        = number
  default     = 2
}

variable "rate_limit_requests" {
  description = "Per-client tracking requests allowed per window (Quac API env)."
  type        = number
  default     = 120
}

variable "rate_limit_window_seconds" {
  description = "Sliding-window length in seconds for the global rate limiter."
  type        = number
  default     = 60
}

variable "webhook_secret" {
  description = "HMAC-SHA256 signing secret for outbound webhook signatures; store in Secrets Manager in real deployments."
  type        = string
  sensitive   = true
  default     = ""
}

variable "secret_key" {
  description = "HMAC pepper for API-key secret hashing (QUAC_SECRET_KEY); store in Secrets Manager in real deployments."
  type        = string
  sensitive   = true
  default     = ""
}

variable "s3_lifecycle_glacier_deep_archive_days" {
  description = "Days before result objects transition to Glacier Deep Archive."
  type        = number
  default     = 30
}

variable "s3_noncurrent_version_expiration_days" {
  description = "Days before noncurrent result object versions are expired."
  type        = number
  default     = 90
}

variable "enable_s3_versioning" {
  description = "Enable object versioning on the results bucket."
  type        = bool
  default     = true
}

variable "common_tags" {
  description = "Tags applied to every taggable Quac resource."
  type        = map(string)
  default = {
    ManagedBy = "terraform"
    Project   = "quac"
    Component = "quantum-maas"
  }
}
