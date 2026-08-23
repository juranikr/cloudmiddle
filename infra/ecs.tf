resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.name_prefix}-api"
  retention_in_days = 14
}

resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  tags = {
    Name = "${local.name_prefix}-cluster"
  }
}

resource "aws_iam_role" "ecs_task_execution" {
  name = "${local.name_prefix}-ecs-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_task_execution_secrets" {
  name = "${local.name_prefix}-ecs-exec-secrets"
  role = aws_iam_role.ecs_task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [
          aws_secretsmanager_secret.app.arn
        ]
      }
    ]
  })
}

resource "aws_iam_role" "ecs_task" {
  name = "${local.name_prefix}-ecs-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

# The scheduled agent uses the Step Functions callback integration to report
# whether a fresh Fargate task should be attempted. Callback APIs do not
# support resource-level permissions, so the token itself is the authority.
resource "aws_iam_role_policy" "ecs_task_step_functions_callback" {
  name = "${local.name_prefix}-ecs-task-sfn-callback"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "states:SendTaskSuccess",
          "states:SendTaskFailure",
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = "${aws_ecr_repository.api.repository_url}:latest"
      essential = true
      portMappings = [
        {
          containerPort = var.container_port
          hostPort      = var.container_port
          protocol      = "tcp"
        }
      ]
      environment = [
        {
          name  = "CORS_ORIGINS"
          value = var.cors_origins
        },
        {
          name  = "AWS_REGION"
          value = var.aws_region
        },
        {
          name  = "S3_BUCKET"
          value = aws_s3_bucket.images.bucket
        },
        {
          name  = "S3_PUBLIC_BASE_URL"
          value = "https://${aws_cloudfront_distribution.images.domain_name}"
        },
        {
          name  = "BRAVE_PLACE_ENABLED"
          value = "true"
        },
        {
          name  = "AGENT_STATE_MACHINE_ARN"
          value = aws_sfn_state_machine.agent.arn
        },
        {
          # Standard Search subscriptions are discovery-only. Keep Brave
          # response fields transient until a storage-rights contract exists.
          name  = "BRAVE_SEARCH_STORAGE_RIGHTS"
          value = "false"
        }
      ]
      secrets = [
        {
          name      = "DATABASE_URL"
          valueFrom = "${aws_secretsmanager_secret.app.arn}:DATABASE_URL::"
        },
        {
          name      = "JWT_SECRET"
          valueFrom = "${aws_secretsmanager_secret.app.arn}:JWT_SECRET::"
        },
        {
          name      = "SEED_PASSWORD_JOOHAN"
          valueFrom = "${aws_secretsmanager_secret.app.arn}:SEED_PASSWORD_JOOHAN::"
        },
        {
          name      = "SEED_PASSWORD_GUKSEO"
          valueFrom = "${aws_secretsmanager_secret.app.arn}:SEED_PASSWORD_GUKSEO::"
        },
        {
          name      = "SEED_PASSWORD_TEST"
          valueFrom = "${aws_secretsmanager_secret.app.arn}:SEED_PASSWORD_TEST::"
        },
        {
          name      = "GROQ_API_KEY"
          valueFrom = "${aws_secretsmanager_secret.app.arn}:GROQ_API_KEY::"
        },
        {
          name      = "GROQ_MODEL"
          valueFrom = "${aws_secretsmanager_secret.app.arn}:GROQ_MODEL::"
        },
        {
          name      = "ARCGIS_API_KEY"
          valueFrom = "${aws_secretsmanager_secret.app.arn}:ARCGIS_API_KEY::"
        },
        {
          name      = "BRAVE_SEARCH_API_KEY"
          valueFrom = "${aws_secretsmanager_secret.app.arn}:BRAVE_SEARCH_API_KEY::"
        }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.api.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "api"
        }
      }
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:${var.container_port}/api/health')\" || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 20
      }
    }
  ])
}

resource "aws_ecs_service" "api" {
  name            = "${local.name_prefix}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = var.container_port
  }

  depends_on = [
    aws_lb_listener.http,
    aws_iam_role_policy.ecs_task_execution_secrets,
    aws_iam_role_policy.ecs_task_agent_state_machine,
  ]

  lifecycle {
    # desired_count는 운영 중 수동 스케일링을 보존한다. 작업 정의는 Terraform이
    # 최신 리비전으로 연결해야 새 환경 변수·Secrets 변경이 서비스에 반영된다.
    ignore_changes = [desired_count]
  }

  tags = {
    Name = "${local.name_prefix}-api"
  }
}
