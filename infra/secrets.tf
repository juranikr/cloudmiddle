resource "aws_secretsmanager_secret" "app" {
  name                    = "${local.name_prefix}/app"
  recovery_window_in_days = var.environment == "prod" ? 7 : 0

  tags = {
    Name = "${local.name_prefix}-app-secret"
  }
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id

  secret_string = jsonencode({
    DATABASE_URL = "postgresql+psycopg2://${var.db_username}:${random_password.db_password.result}@${aws_db_instance.main.address}:5432/${var.db_name}"
    JWT_SECRET   = local.jwt_secret_effective
    DB_PASSWORD  = random_password.db_password.result
  })

  # SEED_PASSWORD_*, GROQ_* 등은 CLI로 추가·유지. terraform apply가 덮어쓰지 않음.
  lifecycle {
    ignore_changes = [secret_string]
  }
}
