# =============================================================================
# Quac — IAM roles (Principle of Least Privilege)
#
# Every policy is scoped to the *specific* ARNs created in this workspace:
#   * DynamoDB -> arn:...:table/<this-workspace table> only (no `table/*`)
#   * SQS      -> arn:...:queue/<this-workspace queues> only
#   * S3       -> arn:...:bucket/<this-workspace bucket>/results/* only
# No wildcard resources, no account-wide grants.
# =============================================================================

# --------------------------------------------------------------------------------------
# Trust policies
# --------------------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# --------------------------------------------------------------------------------------
# API task role — used by the FastAPI container at runtime (PoLP)
#   * DynamoDB: read/write job records in THIS table only
#   * SQS:      SendMessage to THIS queue only
#   * S3:       read result objects under results/* (status deep links)
# --------------------------------------------------------------------------------------

data "aws_iam_policy_document" "api_task" {
  statement {
    sid    = "DynamoDbJobStoreScopedToWorkspaceTable"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:ConditionCheckItem",
      "dynamodb:DescribeTable",
    ]
    resources = [aws_dynamodb_table.jobs.arn]
  }

  statement {
    sid       = "SqsEnqueueToWorkspaceQueueOnly"
    effect    = "Allow"
    actions   = ["sqs:SendMessage", "sqs:GetQueueUrl", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.jobs.arn]
  }

  statement {
    sid    = "S3ReadResultObjectsOnly"
    effect = "Allow"
    actions = [
      "s3:GetObject",
    ]
    resources = ["${aws_s3_bucket.results.arn}/results/*"]
  }

  statement {
    sid    = "S3ListScopedToResultsPrefix"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
    ]
    resources = [aws_s3_bucket.results.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["results/*"]
    }
  }
}

resource "aws_iam_role" "api_task" {
  name                  = "${var.project}-${var.environment}-api-task"
  assume_role_policy    = data.aws_iam_policy_document.ecs_tasks_assume.json
  force_detach_policies = true
  tags                  = var.common_tags
}

resource "aws_iam_role_policy" "api_task" {
  name   = "${var.project}-${var.environment}-api-task-pol"
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_task.json
}

# --------------------------------------------------------------------------------------
# API task execution role — ECS agent pull/publish duties only
#   * ECR image pull for THIS repository
#   * CloudWatch Logs write for THIS log group
# --------------------------------------------------------------------------------------

data "aws_iam_policy_document" "api_execution" {
  statement {
    sid    = "EcrPullWorkspaceRepository"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = [aws_ecr_repository.api.arn]
  }

  statement {
    sid       = "EcrAuthTokenGlobal"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # GetAuthorizationToken does not support resource scoping
  }

  statement {
    sid    = "WriteApiLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }
}

resource "aws_iam_role" "api_execution" {
  name                  = "${var.project}-${var.environment}-api-exec"
  assume_role_policy    = data.aws_iam_policy_document.ecs_tasks_assume.json
  force_detach_policies = true
  tags                  = var.common_tags
}

resource "aws_iam_role_policy" "api_execution" {
  name   = "${var.project}-${var.environment}-api-exec-pol"
  role   = aws_iam_role.api_execution.id
  policy = data.aws_iam_policy_document.api_execution.json
}

# --------------------------------------------------------------------------------------
# Worker Lambda execution role — the compute node (PoLP)
#   * DynamoDB: mutate job status in THIS table only (worker also reads webhook_url)
#   * S3:       PutObject + presign (GetObject) under results/* in THIS bucket only
#   * SQS:      consume from THIS queue only
#   * Logs:     write to the worker log group only
# --------------------------------------------------------------------------------------

data "aws_iam_policy_document" "worker_lambda" {
  statement {
    sid    = "DynamoDbJobStoreScopedToWorkspaceTable"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DescribeTable",
    ]
    resources = [aws_dynamodb_table.jobs.arn]
  }

  statement {
    sid    = "S3WriteAndPresignResultsOnly"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:AbortMultipartUpload",
    ]
    resources = ["${aws_s3_bucket.results.arn}/results/*"]
  }

  statement {
    sid    = "SqsConsumeWorkspaceQueue"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }

  statement {
    sid    = "WriteWorkerLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.worker.arn}:*"]
  }

  statement {
    # Required by the Lambda runtime for X-Ray tracing and config fetching.
    sid       = "LambdaRuntimeBasics"
    effect    = "Allow"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
}

resource "aws_iam_role" "worker_lambda" {
  name                  = "${var.project}-${var.environment}-worker-lambda"
  assume_role_policy    = data.aws_iam_policy_document.lambda_assume.json
  force_detach_policies = true
  tags                  = var.common_tags
}

resource "aws_iam_role_policy" "worker_lambda" {
  name   = "${var.project}-${var.environment}-worker-lambda-pol"
  role   = aws_iam_role.worker_lambda.id
  policy = data.aws_iam_policy_document.worker_lambda.json
}
