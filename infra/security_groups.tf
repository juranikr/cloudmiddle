resource "aws_security_group" "alb" {
  name        = "${local.name_prefix}-alb-sg"
  description = "ALB ingress"
  vpc_id      = aws_vpc.main.id

  lifecycle {
    # Shared listener rules and services rely on this ALB security group.
    prevent_destroy = true
  }

  # CloudFront → ALB (HTTP origin). 인터넷 직접속은 막고 HTTPS는 CloudFront에서 처리
  ingress {
    description     = "HTTP from CloudFront only"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-alb-sg"
  }
}

resource "aws_security_group" "ecs" {
  name        = "${local.name_prefix}-ecs-sg"
  description = "ECS tasks"
  vpc_id      = aws_vpc.main.id

  lifecycle {
    # More than one service uses this task security group.
    prevent_destroy = true
  }

  ingress {
    description     = "From ALB"
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-ecs-sg"
  }
}

resource "aws_security_group" "rds" {
  name        = "${local.name_prefix}-rds-sg"
  description = "Postgres from ECS only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Postgres from ECS"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }

  # description 변경은 SG 교체를 유발하므로 기존 설명 유지 + 인터넷 허용만 추가
  ingress {
    description = "Postgres from anywhere (strong password required)"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    # cloudbali 등 다른 state가 이 공유 DB SG에 접근 규칙을 추가한다.
    # 외부 관리 규칙을 삭제하지 않기 위한 임시 안전장치이며 장기적으로
    # standalone aws_vpc_security_group_ingress_rule 소유권을 분리해야 한다.
    prevent_destroy = true
    ignore_changes  = [ingress]
  }

  tags = {
    Name = "${local.name_prefix}-rds-sg"
  }
}
