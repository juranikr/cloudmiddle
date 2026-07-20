output "aws_account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "aws_region" {
  value = var.aws_region
}

output "vpc_id" {
  value = aws_vpc.main.id
}

output "ecr_repository_url" {
  value = aws_ecr_repository.api.repository_url
}

output "alb_dns_name" {
  value       = aws_lb.api.dns_name
  description = "API 접속 호스트 (임시 HTTP). 예: http://<dns>/api/health"
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  value = aws_ecs_service.api.name
}

output "rds_endpoint" {
  value     = aws_db_instance.main.address
  sensitive = true
}

output "app_secret_arn" {
  value = aws_secretsmanager_secret.app.arn
}

output "github_actions_role_arn" {
  value       = aws_iam_role.github_actions.arn
  description = "GitHub Actions OIDC role — Actions secret AWS_ROLE_ARN 에 넣으세요"
}

output "cloudwatch_log_group" {
  value = aws_cloudwatch_log_group.api.name
}
