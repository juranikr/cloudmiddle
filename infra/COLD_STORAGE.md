# Tourmiddle 장기 일시정지 기록

기준 시각: 2026-10-01 (Asia/Seoul)

## 현재 상태

- `tourmiddle-dev-api`: ECS desired/running `0/0`
- `tourmiddle-dev-agent-daily`: `DISABLED`
- GitHub Actions `ENABLE_AWS_DEPLOY`: `false`
- Terraform `deployment_mode`: `paused`
- 앱 CloudFront는 `/cashcow` 공유 경로 때문에 활성 상태로 유지
- 공유 ALB와 RDS는 AWS deletion protection 및 Terraform `prevent_destroy` 적용

RDS, ALB, VPC, ECS cluster는 cloudbali/cashcow도 사용하는 공유 기반이다.
Tourmiddle만 쉬는 상황에서 이 리소스를 정지하거나 삭제하면 안 된다.

## 보존 자산

- GitHub: `juranikr/cloudmiddle`, 일시정지 전 애플리케이션 HEAD `796093983b25328c37d29ffde44630ec83b7ab80`
- 전용 비공개 보관 버킷: `s3://tourmiddle-archive-155557574983/archives/20261001-002542/`
- 암호화 RDS 스냅샷: `tourmiddle-cold-encrypted-20261001-002542`
- 원본 수동 스냅샷: `tourmiddle-cold-20261001-002542`
- 원본 이미지 버킷: `tourmiddle-dev-place-images-155557574983` (versioning enabled)
- 이미지 사본: 보관 버킷의 `place-images/` prefix
- DB 논리 백업: 보관 버킷의 `artifacts/tourmiddle.dump` 및 `artifacts/cloudbali.dump`
- 소스 사본: Git bundle, main ZIP, 로컬 인프라 설정 ZIP
- Git 미추적 `backend/scripts/` 사본: `artifacts/cloudmiddle-untracked-backend-scripts.zip`
- 애플리케이션 secret: 기존 Secrets Manager secret을 삭제하지 않고 유지

보관 버킷은 S3 기본 AES-256 암호화, versioning, Block Public Access를 사용한다.
Terraform state 버킷은 다른 프로젝트와 공유하므로 별도 삭제 대상이 아니다.

## 검증 결과

PostgreSQL 16 새 컨테이너에 두 custom-format dump를 각각
`pg_restore --single-transaction --exit-on-error`로 복원했다.

| Database | Tables | Rows | Dump SHA-256 | 검증 |
| --- | ---: | ---: | --- | --- |
| tourmiddle | 34 | 31,956 | `E8E23BF85B365F054AD35A5F7C40D65DFDA68783CF33FBA001DA13B225D4223E` | 테이블별 행 수 일치 |
| cloudbali | 36 | 3,683 | `CF65C60EBEDB4670D0F58FA82E9DE46F6C9C2642F272D00919528B4AF6BAD277` | 테이블별 행 수 일치 |

RDS 스냅샷은 PostgreSQL 16.13, 20 GiB이며 암호화 사본이 `available`인 것을
확인했다. 논리 백업과 검증 CSV의 전체 파일 해시는 같은 archive prefix의
`manifest.json`을 기준으로 한다.

## 정상 재개

RDS와 공유 기반이 유지되므로 일반적인 재개에는 DB 복원이 필요 없다.

1. `infra/terraform.tfvars`의 `deployment_mode`를 `active`로 변경한다.
2. `terraform plan`에서 Tourmiddle API `0 -> 1`과 Tourmiddle agent rule 활성화
   외에 공유 리소스 삭제/교체가 없는지 확인한다.
3. `terraform apply` 후 ECS health와 CloudFront 경로를 확인한다.
4. 마지막으로 GitHub repository variable `ENABLE_AWS_DEPLOY=true`를 설정한다.

## 재해 복구

RDS 전체를 잃은 경우에는 암호화 스냅샷으로 PostgreSQL 16 인스턴스를 복원한다.
스냅샷을 사용할 수 없으면 `tourmiddle.dump`와 `cloudbali.dump`를 새 DB에
`pg_restore --single-transaction --exit-on-error --no-owner --no-acl`로 복원한다.
새 endpoint에 맞춰 Secrets Manager의 `DATABASE_URL`만 보존형으로 갱신한 뒤
ECS를 재개한다. 이미지가 손실된 경우 archive `place-images/`를 원본 버킷으로
동기화한다.

공유 기반의 완전 폐기는 cloudbali/cashcow를 각각 백업·중지한 뒤 별도 작업으로
진행해야 한다. `terraform destroy`를 이 저장소에서 바로 실행하지 않는다.
