resource "aws_db_subnet_group" "main" {
  name = "${local.name_prefix}-db-public"
  # 외부 PC 접속용 public subnet (비밀번호로 인증)
  subnet_ids = aws_subnet.public[*].id

  tags = {
    Name = "${local.name_prefix}-db-public"
  }
}

resource "aws_db_instance" "main" {
  identifier = "${local.name_prefix}-postgres"

  engine               = "postgres"
  engine_version       = "16"
  instance_class       = var.db_instance_class
  allocated_storage    = 20
  max_allocated_storage = 100
  storage_type         = "gp3"

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db_password.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = true
  multi_az               = false

  backup_retention_period = var.environment == "prod" ? 7 : 1
  skip_final_snapshot     = var.environment != "prod"
  deletion_protection     = var.enable_deletion_protection

  tags = {
    Name = "${local.name_prefix}-postgres"
  }
}
