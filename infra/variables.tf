variable "project_name" {
  type        = string
  description = "리소스 이름 prefix"
  default     = "tourmiddle"
}

variable "environment" {
  type        = string
  description = "환경 이름 (dev/staging/prod)"
  default     = "dev"
}

variable "deployment_mode" {
  type        = string
  description = "active는 tourmiddle 앱을 실행하고 paused는 공유 기반을 보존한 채 앱만 중지"
  default     = "active"

  validation {
    condition     = contains(["active", "paused"], var.deployment_mode)
    error_message = "deployment_mode must be either active or paused."
  }
}

variable "aws_region" {
  type        = string
  description = "배포 리전 (한국 사용자면 ap-northeast-2 권장)"
  default     = "ap-northeast-2"
}

variable "github_org" {
  type        = string
  description = "GitHub owner/org"
  default     = "juranikr"
}

variable "github_repo" {
  type        = string
  description = "GitHub repository name"
  default     = "cloudmiddle"
}

variable "container_port" {
  type        = number
  description = "FastAPI 컨테이너 포트"
  default     = 8000
}

variable "db_name" {
  type    = string
  default = "tourmiddle"
}

variable "db_username" {
  type    = string
  default = "tourmiddle"
}

variable "api_cpu" {
  type        = number
  description = "ECS task CPU units"
  default     = 256
}

variable "api_memory" {
  type        = number
  description = "ECS task memory (MiB)"
  default     = 512
}

variable "api_desired_count" {
  type        = number
  description = "ECS desired task count"
  default     = 1
}

variable "agent_backend" {
  type        = string
  description = "Agent runtime: groq preserves the legacy path; codex uses the server-side Codex harness"
  default     = "groq"

  validation {
    condition     = contains(["groq", "codex"], var.agent_backend)
    error_message = "agent_backend must be either groq or codex."
  }
}

variable "agent_schedule_enabled" {
  type        = bool
  description = "Enable paid scheduled research only after a manual agent validation; paused always disables it"
  default     = false
}

variable "codex_model" {
  type        = string
  description = "Codex model with verified API access; empty uses the harness default"
  default     = "gpt-6.1-sol"
}

variable "codex_timeout_seconds" {
  type        = number
  description = "Timeout for an individual Codex harness invocation"
  default     = 900

  validation {
    condition     = var.codex_timeout_seconds > 0 && var.codex_timeout_seconds <= 3600 && floor(var.codex_timeout_seconds) == var.codex_timeout_seconds
    error_message = "codex_timeout_seconds must be an integer between 1 and 3600."
  }
}

variable "codex_batch_timeout_seconds" {
  type        = number
  description = "Overall Codex batch timeout; leave headroom below the 14400-second Step Functions task timeout"
  default     = 7200

  validation {
    condition     = var.codex_batch_timeout_seconds > 0 && var.codex_batch_timeout_seconds <= 13500 && floor(var.codex_batch_timeout_seconds) == var.codex_batch_timeout_seconds
    error_message = "codex_batch_timeout_seconds must be an integer between 1 and 13500."
  }
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "enable_deletion_protection" {
  type        = bool
  description = "공유 RDS 삭제 방지. 명시적인 폐기 작업 전까지 true 유지"
  default     = true
}

variable "jwt_secret" {
  type        = string
  description = "앱 JWT 시크릿 (비우면 랜덤 생성)"
  default     = ""
  sensitive   = true
}

variable "cors_origins" {
  type        = string
  description = "쉼표 구분 CORS origins (프론트 CloudFront URL 등)"
  default     = "*"
}
