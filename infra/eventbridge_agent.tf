# KST 03:00 / 11:00 / 19:00 scheduled, outcome-driven city agents.
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
        { name = "AGENT_AUTONOMOUS_RESEARCH", value = "true" },
        { name = "AGENT_MAX_STEPS", value = "180" },
      ]
      secrets = [
        { name = "DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.app.arn}:DATABASE_URL::" },
        { name = "JWT_SECRET", valueFrom = "${aws_secretsmanager_secret.app.arn}:JWT_SECRET::" },
        { name = "GROQ_API_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:GROQ_API_KEY::" },
        { name = "GROQ_MODEL", valueFrom = "${aws_secretsmanager_secret.app.arn}:GROQ_MODEL::" },
        { name = "ARCGIS_API_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:ARCGIS_API_KEY::" },
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

resource "aws_iam_role" "agent_step_functions" {
  name = "${local.name_prefix}-agent-sfn"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "states.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "agent_step_functions" {
  name = "${local.name_prefix}-agent-sfn"
  role = aws_iam_role.agent_step_functions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = [aws_ecs_task_definition.agent.arn]
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:DescribeTasks",
          "ecs:StopTask",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          aws_iam_role.ecs_task_execution.arn,
          aws_iam_role.ecs_task.arn,
        ]
      }
    ]
  })
}

resource "aws_sfn_state_machine" "agent" {
  name     = "${local.name_prefix}-agent"
  role_arn = aws_iam_role.agent_step_functions.arn

  definition = jsonencode({
    Comment = "Run each city agent independently and rotate Fargate egress on retryable provider blocks"
    StartAt = "RunCities"
    States = {
      RunCities = {
        Type           = "Map"
        ItemsPath      = "$.city_ids"
        MaxConcurrency = 1
        ItemSelector = {
          "city_id.$" = "$$.Map.Item.Value"
        }
        ItemProcessor = {
          ProcessorConfig = {
            Mode = "INLINE"
          }
          StartAt = "RunCityAgent"
          States = {
            RunCityAgent = {
              Type           = "Task"
              Resource       = "arn:aws:states:::ecs:runTask.waitForTaskToken"
              TimeoutSeconds = 14400
              Parameters = {
                Cluster        = aws_ecs_cluster.main.arn
                TaskDefinition = aws_ecs_task_definition.agent.arn
                LaunchType     = "FARGATE"
                NetworkConfiguration = {
                  AwsvpcConfiguration = {
                    Subnets        = aws_subnet.public[*].id
                    SecurityGroups = [aws_security_group.ecs.id]
                    AssignPublicIp = "ENABLED"
                  }
                }
                Overrides = {
                  ContainerOverrides = [
                    {
                      Name = "agent"
                      Environment = [
                        {
                          Name      = "SFN_TASK_TOKEN"
                          "Value.$" = "$$.Task.Token"
                        },
                        {
                          Name      = "AGENT_CITY_ID"
                          "Value.$" = "States.Format('{}', $.city_id)"
                        },
                      ]
                    }
                  ]
                }
              }
              Retry = [
                {
                  ErrorEquals     = ["RetryableNetworkBlock", "RetryableModelOutput"]
                  IntervalSeconds = 30
                  BackoffRate     = 3
                  MaxAttempts     = 2
                }
              ]
              End = true
            }
          }
        }
        End = true
      }
    }
  })

  depends_on = [aws_iam_role_policy.agent_step_functions]
}

resource "aws_iam_role" "events_ecs" {
  name = "${local.name_prefix}-events-ecs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sts:AssumeRole"
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
        Action   = ["states:StartExecution"]
        Resource = [aws_sfn_state_machine.agent.arn]
      }
    ]
  })
}

resource "aws_cloudwatch_event_rule" "agent_daily" {
  name                = "${local.name_prefix}-agent-daily"
  description         = "Outcome-driven map curator, three times daily"
  schedule_expression = "cron(0 2,10,18 * * ? *)" # KST 11:00 / 19:00 / 03:00
}

resource "aws_cloudwatch_event_target" "agent_daily" {
  rule      = aws_cloudwatch_event_rule.agent_daily.name
  target_id = "step-functions-agent"
  arn       = aws_sfn_state_machine.agent.arn
  role_arn  = aws_iam_role.events_ecs.arn
  input     = jsonencode({ city_ids = [1, 2] })
}
