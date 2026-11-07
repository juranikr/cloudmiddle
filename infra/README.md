# AWS 인프라 (Terraform)

목표: **다수 사용자 + 에이전트가 CLI/코드로 원격 관리**할 수 있는 환경  
구성: VPC · ALB · ECS Fargate(API) · RDS Postgres · ECR · Secrets Manager · GitHub OIDC

---

## 0) 당신이 먼저 준비할 것 (체크리스트)

순서대로 준비하세요. **1~4번이 끝나기 전에는 `terraform apply` 하지 마세요.**

### 필수

| # | 항목 | 하는 일 | 확인 |
|---|------|---------|------|
| 1 | **AWS 계정** | [aws.amazon.com](https://aws.amazon.com/) 가입, 루트 말고 **IAM 사용자 또는 IAM Identity Center(SSO)** 권장 | ☐ |
| 2 | **결제/한도** | 카드 등록, 프리티어 여부 확인. 이 스택은 대략 **월 $30~80** 수준(RDS·ALB 중심, NAT 없음) | ☐ |
| 3 | **AWS CLI** | PC에 설치 후 `aws configure` 또는 SSO 로그인. `aws sts get-caller-identity` 성공해야 함 | ☐ |
| 4 | **Terraform** | [설치](https://developer.hashicorp.com/terraform/install) `terraform version` ≥ 1.5 | ☐ |
| 5 | **권한** | 해당 IAM에 대략 `AdministratorAccess` (처음) 또는 VPC/ECS/RDS/IAM/ECR/ELB/Secrets 생성 권한 | ☐ |
| 6 | **리전 결정** | 기본값 `ap-northeast-2` (서울). 바꾸려면 `terraform.tfvars` | ☐ |
| 7 | **GitHub 레포** | 이미 `juranikr/cloudmiddle` — Actions 쓰기 권한 있는 계정 | ☐ |

### 권장 (나중에 해도 됨)

| # | 항목 | 설명 |
|---|------|------|
| 8 | **도메인** | 없어도 CloudFront 기본 도메인으로 **HTTPS** 가능. 커스텀 도메인은 ACM+Route53 추가 |
| 9 | **예산 알람** | AWS Billing → Budget $20/$50 알림 |
| 10 | **에이전트용 자격증명** | 로컬: AWS CLI 프로필 / CI: OIDC(테라폼이 역할 생성) |

### 에이전트(Cursor)가 원격 관리하려면

당신 PC 또는 CI에 아래가 되면 됩니다.

1. `aws` CLI 로그인된 상태  
2. `terraform` 설치  
3. `gh` 로그인 (이미 됨)  
4. 이 레포의 `infra/` 수정 → plan/apply → Actions로 이미지 배포  

---

## 1) Bootstrap (state 저장소) — 최초 1회

```powershell
cd infra\bootstrap
terraform init
terraform apply
```

출력의 `backend_hcl_example`을 복사해:

```powershell
cd ..
# backend.hcl 파일 생성 (gitignore 됨)
```

`infra/versions.tf` 의 backend 주석을 해제:

```hcl
backend "s3" {}
```

그다음:

```powershell
cd infra
copy terraform.tfvars.example terraform.tfvars
# 필요시 수정
terraform init -backend-config=backend.hcl
terraform plan
terraform apply
```

---

## 2) apply 직후 할 일

1. 이미지 없으면 ECS가 실패합니다. 먼저 빌드·푸시:

```powershell
# outputs
terraform output ecr_repository_url
aws ecr get-login-password --region ap-northeast-2 | docker login --username AWS --password-stdin <account>.dkr.ecr.ap-northeast-2.amazonaws.com
docker build -t <ecr-url>:latest ../backend
docker push <ecr-url>:latest
aws ecs update-service --cluster <cluster> --service <service> --force-new-deployment
```

2. GitHub 설정 (`juranikr/cloudmiddle` → Settings)

**Secret**
- `AWS_ROLE_ARN` = `terraform output -raw github_actions_role_arn`

**Variables**
- `ENABLE_AWS_DEPLOY` = `true`
- `ECR_REPOSITORY` = ECR 리포지토리 **이름** (URL의 `/` 뒤)
- `ECS_CLUSTER` = `terraform output -raw ecs_cluster_name`
- `ECS_SERVICE` = `terraform output -raw ecs_service_name`

3. API 확인  
`http://<alb_dns_name>/api/health`

---

## 3) 비용·설계 메모

- **NAT Gateway 없음**: ECS를 public subnet + public IP로 두어 ECR pull. 비용 절감용 MVP 선택.
- RDS `db.t4g.micro`, 싱글 AZ.
- 프론트(CloudFront/S3)는 다음 단계. 당분간 Vite 빌드물을 별도 호스팅하거나 ALB 뒤에 정적 서빙 추가 가능.
- prod 전에는 `enable_deletion_protection = true`, Multi-AZ, 권한 축소 검토.

---

## 4) 에이전트에게 시킬 수 있는 작업 예

- `infra/` 변수·보안그룹·스케일 조정 후 `terraform plan`
- GitHub Actions 배포 파이프라인 수정
- CloudWatch 로그 조회 커맨드 작성
- 도메인 + ACM HTTPS 리스너 추가
- 프론트 S3/CloudFront 스택 추가

---

## 5) 장기 미사용 시 안전하게 일시정지

이 state의 ALB, listener, target group, VPC, ECS cluster, RDS는 다른 앱도
참조하는 공유 기반입니다. `terraform destroy` 또는 해당 리소스의 targeted
destroy를 실행하면 안 됩니다. 특히 RDS에는 다른 앱의 운영 데이터도 함께
있으므로 이 프로젝트를 쉬는 동안에도 유지합니다.

`terraform.tfvars`에서 아래 값만 바꿉니다.

```hcl
deployment_mode = "paused"
```

그 후 반드시 plan에서 다음 변경만 있는지 확인하고 apply합니다.

- `tourmiddle-dev-api` ECS service의 `desired_count`: `1 -> 0`
- `tourmiddle-dev-agent-daily` EventBridge rule: `ENABLED -> DISABLED`
- 이미지 bucket versioning 활성화 및 ECR 보존 이미지 수 `10 -> 2`

앱 CloudFront는 `/cashcow` 등 공유 경로의 공개 진입점이므로 paused에서도
활성 상태로 유지합니다. ALB/listener/target group/VPC/ECS cluster/RDS/S3
bucket/Secrets/ECR repository가 삭제 또는 교체로 표시되면 apply하지
않습니다. 코드 push가 불필요한 배포를 일으키지 않도록 GitHub Actions
repository variable `ENABLE_AWS_DEPLOY`도 `false`로 바꿉니다.

공유 ALB와 RDS는 Terraform의 `prevent_destroy`와 AWS 서비스 측 deletion
protection을 함께 사용합니다. 이를 해제하기 전에는 cloudbali/cashcow를 포함한
공유 의존성을 다시 확인해야 합니다. RDS SG의 `ignore_changes = [ingress]`는
다른 state가 관리하는 접근 규칙을 이 state가 삭제하지 않게 하는 임시 안전장치입니다.

재개할 때는 `deployment_mode = "active"`, `agent_schedule_enabled = false`로
설정해 웹/API부터 복구합니다. 정기 agent는 별도 수동 검증 후에만
`agent_schedule_enabled = true`로 활성화합니다. paused에서는 이 값과 관계없이
정기 agent가 비활성화됩니다. ECS service가 정상 상태가 된 뒤
`ENABLE_AWS_DEPLOY=true`와 CloudFront의 `/api/health`를 확인합니다.

2026-10-01 백업 자산과 검증 결과는 [COLD_STORAGE.md](./COLD_STORAGE.md)에
기록되어 있습니다.

## 6) 서버 측 Codex harness로 전환

기본 `agent_backend = "groq"`는 기존 런타임을 유지합니다. Codex가 포함된
새 컨테이너 이미지와 서버용 OpenAI API key 준비가 끝나기 전에는 바꾸지 않습니다.
개인 Codex/ChatGPT 로그인 파일을 이미지에 복사하지 않습니다.

Windows PowerShell에서 저장소 루트 기준으로 아래 helper를 직접 실행합니다.
API key는 숨김 프롬프트로 입력하며 명령행 인자나 tfvars에 넣지 않습니다.

```powershell
.\dev\set-openai-secret.ps1
```

helper는 기존 `tourmiddle-dev/app` JSON의 다른 필드를 보존하고
`OPENAI_API_KEY`만 갱신한 뒤 해당 버전을 다시 읽어 필드 보존을 검증합니다.
임시 JSON은 현재 Windows 사용자만 접근 가능한 임시 디렉터리에 저장하고
항상 정리합니다. Terraform은 secret **값**을 읽거나 관리하지 않습니다.

새 이미지가 ECR에 준비되면 로컬 `terraform.tfvars`를 다음처럼 설정합니다.

```hcl
deployment_mode             = "active"
agent_backend               = "codex"
agent_schedule_enabled      = false
codex_model                 = "gpt-6.1-sol"
codex_timeout_seconds       = 900
codex_batch_timeout_seconds = 7200
```

`codex_model`의 기본값 `gpt-6.1-sol`은 API 모델 목록에서 접근 권한을
확인했습니다. 빈 문자열은 harness의 기본 모델을 사용합니다. API와 batch
task는 `AGENT_BACKEND`, `CODEX_MODEL`,
두 timeout 환경 변수를 받고, Codex 선택 시에만 Secrets Manager의
`OPENAI_API_KEY` 필드를 컨테이너의 `CODEX_API_KEY`로 주입합니다. 기존 Groq,
Brave 및 기타 secret 필드는 제거하지 않습니다. Codex 모드의 API와 batch는
최소 512 CPU units / 1024 MiB를 사용합니다.

`terraform fmt -check`, `terraform validate`, `terraform plan`으로 확인합니다.
허용되는 교체는 **Tourmiddle ECS task definition의 새 revision**이며 ECS
service, 관련 IAM Step Functions 정책/definition의 인플레이스 갱신이 뒤따를
수 있습니다. ALB/listener/target group/VPC/cluster/RDS/bucket/secret/ECR
repository의 삭제 또는 교체가 있으면 apply하지 않습니다. 정기 schedule은
계속 DISABLED여야 합니다. 공유 리소스를 destroy하거나 DB dump로 덮어쓰지
않습니다. 현재 RDS가 유지된 일반 재개에는 DB 복원이 필요 없습니다.

승인된 plan만 apply한 뒤 CloudFront health와 로그인/지도 데이터를 검증하고,
작은 단일 도시 수동 agent 실행으로 인증·모델·도구·비용을 확인합니다.
그 후에만 정기 연구와 GitHub 자동 배포를 각각 활성화합니다.
