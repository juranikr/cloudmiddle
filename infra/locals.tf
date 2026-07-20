locals {
  name_prefix = "${var.project_name}-${var.environment}"

  github_repo_full = "${var.github_org}/${var.github_repo}"

  # ECS가 ECR pull / outbound 할 수 있도록 public subnet + public IP 사용 (NAT 비용 절감)
  # RDS는 private subnet에 두고 SG로 ECS만 허용
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
  }

  jwt_secret_effective = var.jwt_secret != "" ? var.jwt_secret : random_password.jwt_secret.result
}
