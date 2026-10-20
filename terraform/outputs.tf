# =============================================================================
# Quac — outputs
# =============================================================================

output "api_gateway_url" {
  description = "Public HTTPS entry point for the Quac simulate/track endpoints."
  value       = "https://${aws_apigatewayv2_api.http.id}.execute-api.${var.aws_region}.amazonaws.com"
}

output "alb_dns_name" {
  description = "ALB DNS name fronting the FastAPI container (CNAME target for custom domains)."
  value       = aws_lb.api.dns_name
}

output "ecr_api_repository_url" {
  description = "ECR repository to push the Quac API image into."
  value       = aws_ecr_repository.api.repository_url
}

output "ecr_worker_repository_url" {
  description = "ECR repository to push the Quac worker Lambda image into."
  value       = aws_ecr_repository.worker.repository_url
}

output "worker_function_name" {
  description = "Lambda function executing qiskit-aer simulations."
  value       = aws_lambda_function.worker.function_name
}

output "worker_function_arn" {
  description = "ARN of the Quac worker Lambda function."
  value       = aws_lambda_function.worker.arn
}

output "jobs_table_name" {
  description = "DynamoDB table storing Quac job lifecycle records."
  value       = aws_dynamodb_table.jobs.name
}

output "jobs_table_arn" {
  description = "ARN of the Quac job table (consumed by IAM policies)."
  value       = aws_dynamodb_table.jobs.arn
}

output "queue_url" {
  description = "SQS FIFO queue URL for manual publishes / integration tests."
  value       = aws_sqs_queue.jobs.url
}

output "queue_arn" {
  description = "ARN of the primary simulation queue."
  value       = aws_sqs_queue.jobs.arn
}

output "dlq_url" {
  description = "Dead-letter queue URL for forensic replay."
  value       = aws_sqs_queue.dlq.url
}

output "results_bucket_name" {
  description = "S3 bucket holding results/{Y}/{M}/{D}/{job_id}.json objects."
  value       = aws_s3_bucket.results.bucket
}

output "simulate_endpoint" {
  description = "Full URL of the job submission endpoint."
  value       = "https://${aws_apigatewayv2_api.http.id}.execute-api.${var.aws_region}.amazonaws.com/v1/quantum/simulate"
}

output "ecs_cluster_name" {
  description = "ECS cluster running the Quac API service."
  value       = aws_ecs_cluster.quac.name
}

output "cloudwatch_log_groups" {
  description = "Log group names for API, worker and API Gateway."
  value = {
    api    = aws_cloudwatch_log_group.api.name
    worker = aws_cloudwatch_log_group.worker.name
    apigw  = aws_cloudwatch_log_group.apigw.name
  }
}
