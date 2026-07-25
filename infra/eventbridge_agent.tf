# 매일 18:00 UTC (= 한국 03:00) Groq 에이전트 1회 실행
resource "aws_cloudwatch_log_group" "agent" {
  name              = "/ecs/${local.name_prefix}-agent"
  retention_in_days = 14
}

resource "aws_ecs_task_definition" "agent" {
  family                   = "${local.name_prefix}-agent"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "agent"
      image     = "${aws_ecr_repository.api.repository_url}:latest"
      essential = true
      command   = ["python", "-m", "app.agent"]
      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "S3_BUCKET", value = aws_s3_bucket.images.bucket },
        { name = "S3_PUBLIC_BASE_URL", value = "https://${aws_cloudfront_distribution.images.domain_name}" },
      ]
      secrets = [
        { name = "DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.app.arn}:DATABASE_URL::" },
        { name = "JWT_SECRET", valueFrom = "${aws_secretsmanager_secret.app.arn}:JWT_SECRET::" },
        { name = "GROQ_API_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:GROQ_API_KEY::" },
        { name = "GROQ_MODEL", valueFrom = "${aws_secretsmanager_secret.app.arn}:GROQ_MODEL::" },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.agent.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "agent"
        }
      }
    }
  ])
}

resource "aws_iam_role" "events_ecs" {
  name = "${local.name_prefix}-events-ecs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action   = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "events_ecs" {
  name = "${local.name_prefix}-events-ecs"
  role = aws_iam_role.events_ecs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = [aws_ecs_task_definition.agent.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [
          aws_iam_role.ecs_task_execution.arn,
          aws_iam_role.ecs_task.arn,
        ]
      }
    ]
  })
}

resource "aws_cloudwatch_event_rule" "agent_daily" {
  name                = "${local.name_prefix}-agent-daily"
  description         = "Daily Groq map curator"
  schedule_expression = "cron(0 18 * * ? *)" # 03:00 KST
}

resource "aws_cloudwatch_event_target" "agent_daily" {
  rule      = aws_cloudwatch_event_rule.agent_daily.name
  target_id = "ecs-agent"
  arn       = aws_ecs_cluster.main.arn
  role_arn  = aws_iam_role.events_ecs.arn

  ecs_target {
    task_count          = 1
    task_definition_arn = aws_ecs_task_definition.agent.arn
    launch_type         = "FARGATE"
    network_configuration {
      subnets          = aws_subnet.public[*].id
      security_groups  = [aws_security_group.ecs.id]
      assign_public_ip = true
    }
  }
}
