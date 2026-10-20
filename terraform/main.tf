# =============================================================================
# Quac — root module: provider, networking, API (ECS Fargate), worker (Lambda)
#
# Topology
#   Client -> HTTP API Gateway (VPC link) -> ALB -> FastAPI on ECS Fargate
#              -> DynamoDB (job record) -> SQS FIFO -> Lambda worker (qiskit-aer)
#              -> S3 results bucket -> signed webhook callback
# =============================================================================

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0" # AWS provider v5.x+
    }
  }

  # Remote state example (uncomment and configure for team use):
  # backend "s3" {
  #   bucket         = "quac-tfstate"
  #   key            = "quantum-maas/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "quac-tf-locks"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = var.common_tags
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_ecr_image" "api_latest" {
  repository_name = aws_ecr_repository.api.name
  image_tag       = var.api_image_tag
}

# --------------------------------------------------------------------------------------
# Networking — VPC with public subnets (ALB) and private subnets (Fargate)
# --------------------------------------------------------------------------------------

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.13"

  name = "${var.project}-${var.environment}-vpc"
  cidr = var.vpc_cidr

  azs             = var.availability_zones
  public_subnets  = [for i, cidr in ["10.42.0.0/24", "10.42.1.0/24"] : cidr]
  private_subnets = [for i, cidr in ["10.42.10.0/24", "10.42.11.0/24"] : cidr]

  enable_nat_gateway     = true                      # Fargate tasks pull ECR images via NAT
  single_nat_gateway     = var.environment != "prod" # one NAT in non-prod to cut cost
  one_nat_gateway_per_az = var.environment == "prod"

  enable_dns_hostnames = true
  enable_dns_support   = true

  public_subnet_tags = {
    "kubernetes.io/role/elb" = "1" # discoverable if a cluster is added later
  }

  tags = var.common_tags
}

# --------------------------------------------------------------------------------------
# ECR repositories
# --------------------------------------------------------------------------------------

resource "aws_ecr_repository" "api" {
  name                 = "${var.project}/${var.environment}/api"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = merge(var.common_tags, { Role = "api-image" })
}

resource "aws_ecr_repository" "worker" {
  name                 = "${var.project}/${var.environment}/worker"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  encryption_configuration {
    encryption_type = "AES256"
  }

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = merge(var.common_tags, { Role = "worker-image" })
}

# --------------------------------------------------------------------------------------
# CloudWatch log groups
# --------------------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "api" {
  name              = "/quac/${var.environment}/api"
  retention_in_days = var.log_retention_days
  tags              = var.common_tags
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/quac/${var.environment}/worker"
  retention_in_days = var.log_retention_days
  tags              = var.common_tags
}

# --------------------------------------------------------------------------------------
# Application Load Balancer (internet-facing, public subnets)
# --------------------------------------------------------------------------------------

resource "aws_lb" "api" {
  name               = "${var.project}-${var.environment}-api"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = module.vpc.public_subnets

  enable_deletion_protection = var.environment == "prod"
  drop_invalid_header_fields = true

  tags = merge(var.common_tags, { Role = "api-alb" })
}

resource "aws_security_group" "alb" {
  name        = "${var.project}-${var.environment}-alb"
  description = "Quac API ALB — ingress 443/80, egress to Fargate tasks"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description = "HTTPS from internet"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP from internet (redirects to HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Forward to Fargate tasks"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [module.vpc.vpc_cidr_block]
  }

  tags = merge(var.common_tags, { Name = "${var.project}-${var.environment}-alb" })
}

resource "aws_security_group" "fargate" {
  name        = "${var.project}-${var.environment}-fargate"
  description = "Quac API Fargate tasks — ingress 8000 from ALB only"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description     = "FastAPI from ALB"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "HTTPS out (DynamoDB/SQS endpoints, AWS APIs via NAT)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.common_tags, { Name = "${var.project}-${var.environment}-fargate" })
}

resource "aws_lb_target_group" "api" {
  name        = "${var.project}-${var.environment}-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = module.vpc.vpc_id

  health_check {
    enabled             = true
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  deregistration_delay = 30

  tags = var.common_tags
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.api_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# --------------------------------------------------------------------------------------
# ECS Fargate cluster + Quac API service
# --------------------------------------------------------------------------------------

resource "aws_ecs_cluster" "quac" {
  name = "${var.project}-${var.environment}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = var.common_tags
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.project}-${var.environment}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.api_execution.arn
  task_role_arn            = aws_iam_role.api_task.arn

  container_definitions = jsonencode([
    {
      name      = "quac-api"
      image     = "${aws_ecr_repository.api.repository_url}:${var.api_image_tag}"
      essential = true

      portMappings = [
        {
          containerPort = 8000
          hostPort      = 8000
          protocol      = "tcp"
        }
      ]

      environment = [
        { name = "QUAC_JOBS_TABLE", value = aws_dynamodb_table.jobs.name },
        { name = "QUAC_QUEUE_URL", value = aws_sqs_queue.jobs.url },
        { name = "QUAC_AWS_REGION", value = var.aws_region },
        { name = "QUAC_LOG_LEVEL", value = "INFO" },
        { name = "QUAC_RATE_LIMIT_REQUESTS", value = tostring(var.rate_limit_requests) },
        { name = "QUAC_RATE_LIMIT_WINDOW", value = tostring(var.rate_limit_window_seconds) },
        { name = "WEB_CONCURRENCY", value = "4" },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.api.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "api"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "curl -fsS http://localhost:8000/healthz || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 20
      }
    }
  ])

  tags = var.common_tags
}

resource "aws_ecs_service" "api" {
  name            = "${var.project}-${var.environment}-api"
  cluster         = aws_ecs_cluster.quac.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  platform_version = "LATEST"

  network_configuration {
    subnets          = module.vpc.private_subnets
    security_groups  = [aws_security_group.fargate.id]
    assign_public_ip = false # private subnets + NAT egress
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "quac-api"
    container_port   = 8000
  }

  deployment_minimum_healthy_percent = 50
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 60

  depends_on = [aws_lb_listener.https]

  tags = var.common_tags
}

# --------------------------------------------------------------------------------------
# HTTP API Gateway fronting the ALB via VPC Link
# --------------------------------------------------------------------------------------

resource "aws_apigatewayv2_api" "http" {
  name          = "${var.project}-${var.environment}-http"
  protocol_type = "HTTP"
  description   = "Quac public entry point — routes to the FastAPI container via ALB"

  cors_configuration {
    allow_headers = ["content-type", "authorization", "x-requested-with"]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_origins = ["*"] # tighten to consumer domains in production
    max_age       = 300
  }

  tags = var.common_tags
}

resource "aws_apigatewayv2_vpc_link" "alb" {
  name               = "${var.project}-${var.environment}-alb"
  security_group_ids = [aws_security_group.vpclink.id]
  subnet_ids         = module.vpc.private_subnets

  tags = var.common_tags
}

resource "aws_security_group" "vpclink" {
  name        = "${var.project}-${var.environment}-vpclink"
  description = "Quac API Gateway VPC link — egress to ALB on 443"
  vpc_id      = module.vpc.vpc_id

  egress {
    description = "HTTPS to ALB"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [module.vpc.vpc_cidr_block]
  }

  tags = merge(var.common_tags, { Name = "${var.project}-${var.environment}-vpclink" })
}

resource "aws_apigatewayv2_integration" "alb" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "HTTP_PROXY"
  integration_uri        = aws_lb_listener.https.arn
  payload_format_version = "1.0"
  integration_method     = "ANY"

  connection_type = "VPC_LINK"
  connection_id   = aws_apigatewayv2_vpc_link.alb.id
  request_parameters = {
    "overwrite:path" = "$request.path"
  }
}

resource "aws_apigatewayv2_route" "simulate" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "POST /v1/quantum/simulate"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

resource "aws_apigatewayv2_route" "jobs" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /v1/quantum/jobs/{job_id}"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

resource "aws_apigatewayv2_route" "formats" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /v1/quantum/formats"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

resource "aws_apigatewayv2_route" "health" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /healthz"
  target    = "integrations/${aws_apigatewayv2_integration.alb.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = var.rate_limit_requests
    throttling_rate_limit  = var.rate_limit_requests / max(var.rate_limit_window_seconds, 1)
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      responseLength = "$context.responseLength"
      integrationErr = "$context.integration.error"
    })
  }

  tags = var.common_tags
}

resource "aws_cloudwatch_log_group" "apigw" {
  name              = "/quac/${var.environment}/apigw"
  retention_in_days = var.log_retention_days
  tags              = var.common_tags
}

# --------------------------------------------------------------------------------------
# Worker — Lambda (container image) consuming the FIFO queue
# --------------------------------------------------------------------------------------

resource "aws_lambda_function" "worker" {
  function_name = "${var.project}-${var.environment}-worker"
  description   = "Quac qiskit-aer compute worker — consumes simulation jobs from SQS FIFO"
  role          = aws_iam_role.worker_lambda.arn

  package_type  = "Image"
  image_uri     = var.worker_image_uri
  architectures = [var.worker_architecture] # must match Dockerfile.worker wheels

  memory_size = var.worker_memory_size
  timeout     = var.worker_timeout_seconds # 600 s max; internal guard fires at 570 s

  ephemeral_storage {
    size = 2048 # qiskit-aer scratch space for large statevectors
  }

  environment {
    variables = {
      QUAC_JOBS_TABLE            = aws_dynamodb_table.jobs.name
      QUAC_RESULTS_BUCKET        = aws_s3_bucket.results.bucket
      QUAC_AWS_REGION            = var.aws_region
      QUAC_LOG_LEVEL             = "INFO"
      QUAC_TIMEOUT_GUARD_SECONDS = "570" # 9:30 of the 10:00 lifecycle
      QUAC_WEBHOOK_SECRET        = var.webhook_secret
      QUAC_PRESIGNED_TTL         = "900"
    }
  }

  tracing_config {
    mode = "Active" # X-Ray active tracing for latency forensics
  }

  depends_on = [aws_cloudwatch_log_group.worker]

  tags = var.common_tags
}

resource "aws_lambda_event_source_mapping" "sqs" {
  event_source_arn = aws_sqs_queue.jobs.arn
  function_name    = aws_lambda_function.worker.arn

  batch_size                         = 5
  maximum_batching_window_in_seconds = 5

  # Partial batch failure reporting: the handler returns batchItemFailures so
  # only infrastructure-failed records are redelivered; business failures are
  # acknowledged and never retried.
  function_response_types = ["ReportBatchItemFailures"]

  enabled = true

  depends_on = [aws_sqs_queue_policy.jobs]
}

# --------------------------------------------------------------------------------------
# Observability — worker error-rate alarm
# --------------------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "worker_errors" {
  alarm_name          = "${var.project}-${var.environment}-worker-errors"
  alarm_description   = "Quac: worker Lambda errors above threshold"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 3
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.worker.function_name
  }

  tags = var.common_tags
}
