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
  description = "ALB DNS (CloudFront origin 전용, 직접 접속 비권장)"
}

output "app_url" {
  value       = "https://${aws_cloudfront_distribution.app.domain_name}"
  description = "HTTPS 앱 URL (CloudFront)"
}

output "cloudfront_domain_name" {
  value = aws_cloudfront_distribution.app.domain_name
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.app.id
}

output "images_bucket" {
  value = aws_s3_bucket.images.bucket
}

output "images_cdn_url" {
  value = "https://${aws_cloudfront_distribution.images.domain_name}"
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
