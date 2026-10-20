# =============================================================================
# Quac — SQS FIFO queue + dead-letter queue
#
# FIFO guarantees strict ordering per MessageGroupId (the API sets the group
# to job_id) and, with Content-Based Deduplication enabled, collapses
# re-submissions of identical circuit payloads — the API additionally sends an
# explicit MessageDeduplicationId derived from the idempotency key.
#
# Redrive policy: after maxReceiveCount (3) failed receives the message moves
# to the DLQ for forensic replay instead of cycling forever.
#
# Visibility timeout MUST exceed the Lambda timeout (600 s): 660 s leaves a
# 60 s buffer so an in-flight job is never redelivered while still running.
# =============================================================================

# Customer-managed KMS key for queue SSE. A CMK (rather than alias/aws/sqs)
# enables rotation, key-level IAM segregation, and per-key CloudTrail audit.
resource "aws_kms_key" "sqs" {
  description             = "${var.project} ${var.environment} — SQS FIFO server-side encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  tags = merge(var.common_tags, {
    Name = "${var.project}-${var.environment}-sqs"
    Role = "queue-encryption"
  })
}

resource "aws_kms_alias" "sqs" {
  name          = "alias/${var.project}-${var.environment}-sqs"
  target_key_id = aws_kms_key.sqs.key_id
}

resource "aws_sqs_queue" "dlq" {
  name                        = "${var.project}-${var.environment}-jobs-dlq.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
  message_retention_seconds   = 1209600             # 14 days for forensic replay
  kms_master_key_id           = aws_kms_key.sqs.arn # SSE with workspace CMK

  tags = merge(var.common_tags, {
    Name = "${var.project}-${var.environment}-jobs-dlq.fifo"
    Role = "dead-letter"
  })
}

resource "aws_sqs_queue" "jobs" {
  name                        = "${var.project}-${var.environment}-jobs.fifo"
  fifo_queue                  = true
  content_based_deduplication = true                               # broker-side dedup fallback for identical bodies
  visibility_timeout_seconds  = var.sqs_visibility_timeout_seconds # 660 > Lambda 600
  message_retention_seconds   = var.sqs_message_retention_seconds
  receive_wait_time_seconds   = 20 # long-polling: fewer empty receives, lower cost
  kms_master_key_id           = aws_kms_key.sqs.arn

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.sqs_max_receive_count
  })

  tags = merge(var.common_tags, {
    Name = "${var.project}-${var.environment}-jobs.fifo"
    Role = "simulation-jobs"
  })
}

# Restrict queue access to the Quac API (send) and worker (consume) roles only.
resource "aws_sqs_queue_policy" "jobs" {
  queue_url = aws_sqs_queue.jobs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "QuacEnqueueFromApiRole"
        Effect = "Allow"
        Principal = {
          AWS = aws_iam_role.api_task.arn
        }
        Action   = ["sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
        Resource = aws_sqs_queue.jobs.arn
      },
      {
        Sid    = "QuacConsumeFromWorkerRole"
        Effect = "Allow"
        Principal = {
          AWS = aws_iam_role.worker_lambda.arn
        }
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:ChangeMessageVisibility",
        ]
        Resource = aws_sqs_queue.jobs.arn
      },
      {
        Sid       = "QuacDenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:*"
        Resource  = aws_sqs_queue.jobs.arn
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
    ]
  })
}

resource "aws_sqs_queue_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "QuacDlqAllowRedrive"
        Effect = "Allow"
        Principal = {
          AWS = aws_iam_role.worker_lambda.arn
        }
        Action   = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.dlq.arn
      },
      {
        Sid       = "QuacDlqDenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:*"
        Resource  = aws_sqs_queue.dlq.arn
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
    ]
  })
}

# Alarm when anything lands in the DLQ — a poisoned message or a systemic
# worker failure. Wire the alarm action to an SNS topic for paging.
resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  alarm_name          = "${var.project}-${var.environment}-dlq-not-empty"
  alarm_description   = "Quac: messages arrived in the simulation dead-letter queue"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.dlq.name
  }

  tags = var.common_tags
}
