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
