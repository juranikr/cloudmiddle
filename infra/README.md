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
| 7 | **GitHub 레포** | 이미 `juranikr/tourmiddle` — Actions 쓰기 권한 있는 계정 | ☐ |

### 권장 (나중에 해도 됨)

| # | 항목 | 설명 |
|---|------|------|
| 8 | **도메인** | 예: `api.example.com` — ACM 인증서 + HTTPS. 없어도 ALB DNS로 HTTP 테스트 가능 |
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

2. GitHub 설정 (`juranikr/tourmiddle` → Settings)

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
