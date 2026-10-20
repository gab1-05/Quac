# =============================================================================
# Quac — DynamoDB job store
#
# Billing-mode rationale
# ----------------------
# Quac's access pattern is a textbook on-demand workload: job writes are
# bursty (each submission produces 2-3 writes), reads are single-digit QPS
# from the tracking endpoint, and traffic has no predictable diurnal shape —
# it is driven by consumer batch schedules. PAY_PER_REQUEST therefore costs
# less than provisioned + autoscaling and removes the risk of
# ThrottlingException during cold campaign spikes.
#
# If a steady, known baseline ever emerges (>= ~50% sustained RCU/WCU
# utilisation), switch by setting billing_mode = "PROVISIONED" with
# read_capacity/write_capacity values and uncommenting the autoscaling
# resources below (they are fully written out and ready to use).
# =============================================================================

resource "aws_dynamodb_table" "jobs" {
  name         = "${var.project}-${var.environment}-jobs"
  billing_mode = "PAY_PER_REQUEST" # on-demand: no capacity planning, no throttling
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  # Owner index: tenant-scoped job listings query by owner without a table
  # scan. Pre-auth records carry owner_id = "system" (sparse-index safe: items
  # lacking the attribute are simply absent from the index until backfilled).
  attribute {
    name = "owner_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = "owner_id-index"
    hash_key        = "owner_id"
    range_key       = "created_at"
    projection_type = "ALL" # jobs are small; full projection avoids follow-up reads
  }

  # Point-in-time recovery: continuous backups for the last 35 days, enabling
  # restore to any second in the window — protects job state from accidental
  # DeleteItem/UpdateItem storms and software defects.
  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true # AWS-owned KMS key (SQSSE-S3) at rest
  }

  # Job records are ephemeral operational state; TTL lets DynamoDB reclaim
  # them without an application-side sweeper.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = merge(var.common_tags, {
    Name    = "${var.project}-${var.environment}-jobs"
    Role    = "job-store"
    Project = var.project
  })
}

# -----------------------------------------------------------------------------
# Optional provisioned-capacity path (uncomment alongside billing_mode switch)
# -----------------------------------------------------------------------------

# resource "aws_appautoscaling_target" "jobs_read" {
#   max_capacity       = 100
#   min_capacity       = 5
#   resource_id        = "table/${aws_dynamodb_table.jobs.name}"
#   scalable_dimension = "dynamodb:table:ReadCapacityUnits"
#   service_namespace  = "dynamodb"
# }
#
# resource "aws_appautoscaling_policy" "jobs_read" {
#   name               = "${var.project}-${var.environment}-jobs-read-scaling"
#   policy_type        = "TargetTrackingScaling"
#   resource_id        = aws_appautoscaling_target.jobs_read.resource_id
#   scalable_dimension = aws_appautoscaling_target.jobs_read.scalable_dimension
#   service_namespace  = aws_appautoscaling_target.jobs_read.service_namespace
#
#   target_tracking_scaling_policy_configuration {
#     target_value       = 70.0
#     scale_in_cooldown  = 60
#     scale_out_cooldown = 60
#
#     predefined_metric_specification {
#       predefined_metric_type = "DynamoDBReadCapacityUtilization"
#     }
#   }
# }
#
# resource "aws_appautoscaling_target" "jobs_write" {
#   max_capacity       = 100
#   min_capacity       = 5
#   resource_id        = "table/${aws_dynamodb_table.jobs.name}"
#   scalable_dimension = "dynamodb:table:WriteCapacityUnits"
#   service_namespace  = "dynamodb"
# }
#
# resource "aws_appautoscaling_policy" "jobs_write" {
#   name               = "${var.project}-${var.environment}-jobs-write-scaling"
#   policy_type        = "TargetTrackingScaling"
#   resource_id        = aws_appautoscaling_target.jobs_write.resource_id
#   scalable_dimension = aws_appautoscaling_target.jobs_write.scalable_dimension
#   service_namespace  = aws_appautoscaling_target.jobs_write.service_namespace
#
#   target_tracking_scaling_policy_configuration {
#     target_value       = 70.0
#     scale_in_cooldown  = 60
#     scale_out_cooldown = 60
#
#     predefined_metric_specification {
#       predefined_metric_type = "DynamoDBWriteCapacityUtilization"
#     }
#   }
# }
