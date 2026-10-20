# =============================================================================
# Quac — S3 result asset bucket
#
# Security posture:
#   * All four public-access blocks enabled (the bucket is never reachable
#     anonymously, regardless of ACL or bucket-policy mistakes).
#   * SSE-KMS with a customer-managed key (key rotation, per-key IAM and
#     CloudTrail audit) as the default encryption on every object.
#   * Bucket policy denies any request not using TLS (aws:SecureTransport).
#   * Lifecycle: STANDARD -> GLACIER_DEEP_ARCHIVE after 30 days. Results are
#     write-once reference data; hours-long retrieval latency is acceptable
#     for an archive tier and Deep Archive is the cheapest storage class.
# =============================================================================

# Customer-managed KMS key for result-object encryption.
resource "aws_kms_key" "results" {
  description             = "${var.project} ${var.environment} — S3 result-object SSE"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  tags = merge(var.common_tags, {
    Name = "${var.project}-${var.environment}-results"
    Role = "result-encryption"
  })
}

resource "aws_kms_alias" "results" {
  name          = "alias/${var.project}-${var.environment}-results"
  target_key_id = aws_kms_key.results.key_id
}

resource "aws_s3_bucket" "results" {
  bucket        = "${var.project}-${var.environment}-results-${data.aws_caller_identity.current.account_id}"
  force_destroy = false # protect simulation history from accidental destroy

  tags = merge(var.common_tags, {
    Name = "${var.project}-${var.environment}-results"
    Role = "result-store"
  })
}

resource "aws_s3_bucket_public_access_block" "results" {
  bucket = aws_s3_bucket.results.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms" # SSE-KMS with workspace CMK
      kms_master_key_id = aws_kms_key.results.arn
    }
    bucket_key_enabled = true # halves KMS request costs for repeated GETs
  }
}

resource "aws_s3_bucket_versioning" "results" {
  bucket = aws_s3_bucket.results.id

  versioning_configuration {
    status = var.enable_s3_versioning ? "Enabled" : "Suspended"
  }
}

resource "aws_s3_bucket_ownership_controls" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    object_ownership = "BucketOwnerEnforced" # disable ACLs entirely
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "results" {
  bucket = aws_s3_bucket.results.id
  depends_on = [
    aws_s3_bucket_versioning.results
  ]

  rule {
    id     = "quac-glacier-deep-archive-30d"
    status = "Enabled"

    # Applies to the results/{year}/{month}/{day}/{job_id}.json prefix.
    filter {
      prefix = "results/"
    }

    transition {
      days          = var.s3_lifecycle_glacier_deep_archive_days
      storage_class = "DEEP_ARCHIVE" # cheapest tier after 30 days
    }

    noncurrent_version_expiration {
      noncurrent_days = var.s3_noncurrent_version_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_policy" "results" {
  bucket = aws_s3_bucket.results.id
  depends_on = [
    aws_s3_bucket_public_access_block.results
  ]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.results.arn,
          "${aws_s3_bucket.results.arn}/*",
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
      {
        Sid    = "AllowLeastPrivilegeAccessToResultsPrefix"
        Effect = "Allow"
        Principal = {
          AWS = [
            aws_iam_role.worker_lambda.arn, # write results + presign GETs
            aws_iam_role.api_task.arn       # status-link reads
          ]
        }
        Action = [
          "s3:GetObject",
          "s3:PutObject",
        ]
        Resource = "${aws_s3_bucket.results.arn}/results/*"
      },
    ]
  })
}
