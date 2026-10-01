resource "aws_lb" "api" {
  name                       = "${local.name_prefix}-alb"
  internal                   = false
  load_balancer_type         = "application"
  enable_deletion_protection = true
  idle_timeout               = 180
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.public[*].id

  lifecycle {
    # Other applications route through this shared load balancer.
    prevent_destroy = true
  }

  tags = {
    Name = "${local.name_prefix}-alb"
  }
}

resource "aws_lb_target_group" "api" {
  name        = "${local.name_prefix}-api-tg"
  port        = var.container_port
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  lifecycle {
    # This is the shared listener's default target group.
    prevent_destroy = true
  }

  health_check {
    enabled             = true
    path                = "/api/health"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }

  tags = {
    Name = "${local.name_prefix}-api-tg"
  }
}

# 도메인/ACM 붙이기 전까지 HTTP로 서비스 (나중에 443 + redirect 추가)
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  lifecycle {
    # Listener rules owned by other states are attached here.
    prevent_destroy = true
  }

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
