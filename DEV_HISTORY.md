# cloudmiddle / 지난 여행 지도 — 개발 히스토리 (Living Doc)

> **이 파일은 프로젝트의 단일 컨텍스트 소스입니다.**  
> Cursor 에이전트는 작업 시작 전 반드시 읽고, 요청·수정이 끝날 때마다 갱신한 뒤 GitHub `main`에 push 합니다.  
> 규칙: `.cursor/rules/dev-history.mdc`

최종 갱신: 2026-07-25 (KST) — 따종/고덕 공유 가져오기

---

## 1) 한 줄 요약

한국 여행자를 위한 **중국 지난(济南) 중심 공유 여행 지도** 웹앱.  
핀/구역 마커, JWT 로그인, 로컬 SQLite / AWS Postgres, CloudFront HTTPS로 원격 서비스 중.

---

## 2) 현재 접속·인프라 (운영)

| 항목 | 값 |
|------|-----|
| **HTTPS 앱 URL** | https://d232kzujcg4ufp.cloudfront.net |
| GitHub | https://github.com/juranikr/cloudmiddle (`main`) |
| AWS 계정 | `155557574983` |
| 리전 | `ap-northeast-2` |
| 리소스 prefix | `tourmiddle-dev-*` (프로젝트명 tourmiddle, 레포명은 cloudmiddle) |
| CloudFront | `d232kzujcg4ufp.cloudfront.net` / ID `E8KHXFSNPD4UR` |
| ALB (origin 전용) | `tourmiddle-dev-alb-295541249.ap-northeast-2.elb.amazonaws.com` |
| ECR | `…/tourmiddle-dev-api` |
| ECS | cluster `tourmiddle-dev-cluster` / service `tourmiddle-dev-api` |
| RDS | `tourmiddle-dev-postgres…` (Postgres 16, `db.t4g.micro`) |
| GitHub OIDC role | `arn:aws:iam::155557574983:role/tourmiddle-dev-github-actions` |
| TF state | S3 `tourmiddle-tfstate-155557574983` + DynamoDB `tourmiddle-tf-lock` |

**트래픽 경로:** 브라우저 → CloudFront(HTTPS) → ALB(HTTP, CloudFront prefix만 허용) → ECS Fargate → RDS

**대략 월 비용 (24/7):** 약 $45–60 (ALB+RDS+Fargate 중심, NAT 없음). CloudFront는 소량 트래픽이면 소액 추가.

---

## 3) 제품·기능

- 지도: Leaflet + OSM, 중심 지난
- 마커: point(핀) / polygon(구역), 카테고리: tourist, lodging, restaurant, transport, shopping, drink, convenience, other
- UX: 핀 모드 → 드래프트 + ConfirmBar(입력/취소); 구역 모드 → 탭 선택(점-in-폴리곤), 드래그로 그리기; 핀 모드에서는 구역이 클릭을 가로채지 않음
- 공유 가시성 + 개인 필터
- 주소 검색: 백엔드 `/api/geocode` → Nominatim
- 위치: HTTPS/localhost에서 GPS; HTTP LAN(아이폰)은 지도 중심 **가상 위치**
- 지도 뷰: center/zoom·locate 플래그 `localStorage` 유지
- 마커 설명: `http(s)://`·`www.` URL 자동 링크 (보기 모드, 새 탭)
- **따종·고덕 가져오기**: 상단 붙여넣기 → `/api/import/share`
  - 고덕 `surl.amap.com`: 리다이렉트 쿼리에서 명칭·주소·좌표 추출, GCJ-02→WGS84 후 핀+작성 폼
  - 따종 공유 문구: `【이름】`·평점·가격·주소·`dpurl.cn` 파싱. 좌표는 로그인벽으로 불가 → **지도 탭으로 위치 지정** (이름/설명/링크는 자동)
- 인증: JWT. 시드 계정 `alice@test.com` / `bob@test.com` / `carol@test.com` / 비밀번호 `test1234`

---

## 4) 스택·디렉터리

```
cloudmiddle/
  DEV_HISTORY.md          ← 이 파일 (컨텍스트 소스)
  .cursor/rules/          ← Cursor 강제 규칙
  Dockerfile              ← multi-stage: FE build + FastAPI가 static 서빙
  backend/app/            ← FastAPI (main, auth, models, geocode, seed, …)
  frontend/src/           ← React/Vite (MapPage, ZoneDrawer, …)
  infra/                  ← Terraform (VPC/ALB/ECS/RDS/ECR/OIDC/CloudFront)
  .github/workflows/deploy-api.yml
```

- 로컬 DB 기본: SQLite `backend/jinan_travel.db` (Docker Desktop 불필요)
- 프로덕션: Postgres (Secrets Manager의 `DATABASE_URL`)
- FE API base: 배포 시 같은 오리진 `""` (`frontend/src/api.ts`)

---

## 5) 로컬 개발 (다른 PC에서)

```powershell
# Backend (Python 3.11 권장)
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Frontend
cd frontend
npm install
npm run dev
# http://localhost:5173
```

상세: 루트 `README.md`, AWS: `infra/README.md`

---

## 6) 배포 방법

1. 코드 push → GitHub Actions `Deploy to AWS ECS` (vars: `ENABLE_AWS_DEPLOY=true`, `ECR_REPOSITORY`, `ECS_CLUSTER`, `ECS_SERVICE`; secret: `AWS_ROLE_ARN`)
2. 이미지: repo root `Dockerfile` → ECR `:latest` + sha → ECS force deploy
3. 인프라 변경: `infra/` 에서 `terraform apply` (로컬 `backend.hcl` / `terraform.tfvars`는 gitignore)

**OIDC 주의:** GitHub org에 subject 커스텀이 켜져 있음.  
실제 sub 예: `repo:juranikr@295397696/cloudmiddle@1306725754:ref:refs/heads/main`  
IAM trust는 `repo:juranikr/cloudmiddle:*` **와** `repo:juranikr@*/cloudmiddle@*:*` 둘 다 허용해야 함.  
`infra/terraform.tfvars`의 `github_repo`는 **`cloudmiddle`**(레포명). `project_name`만 `tourmiddle`.

---

## 7) 히스토리 타임라인

1. **아맵/디엔핑 스크래핑** — 한국에서 비현실적 → 자체 지난 여행 지도 앱으로 전환
2. **MVP** — FastAPI + React + Leaflet, 공유 마커/구역, 3 테스트 계정, Docker Compose(Postgres) 옵션
3. **로컬 Windows** — Docker Desktop 불안정 → SQLite + venv/conda, LAN 모바일, 가상 위치 UX
4. **지도 UX** — 핀/구역 모드, ConfirmBar, 검색, 뷰 유지
5. **AWS Terraform** — VPC, ALB, ECS Fargate, RDS, ECR, Secrets, GitHub OIDC, bootstrap state
6. **원격 배포** — root multi-stage Dockerfile, SPA를 FastAPI static 서빙, Actions로 ECR/ECS
7. **OIDC 장애** — role 이름 `tourmiddle` vs `cloudmiddle` 혼동 + GitHub sub에 org/repo ID 포함 → trust 수정 후 배포 성공
8. **HTTPS** — CloudFront 기본 인증서, ALB는 CloudFront prefix list만 허용. URL: https://d232kzujcg4ufp.cloudfront.net
9. **컨텍스트 파일** — `DEV_HISTORY.md` + alwaysApply 규칙 (이 문서)

---

## 8) 보안·비밀 (커밋 금지)

- `infra/terraform.tfvars`, `infra/backend.hcl`, `infra/outputs.json`, `*.tfstate`, `.env`, DB 파일
- Access Key를 채팅에 붙인 적 있음 → 가능하면 로테이션
- RDS 비밀번호·JWT는 Secrets Manager (`tourmiddle-dev/app`)

---

## 9) 의도적으로 안 한 것 / 다음 후보

- 커스텀 도메인 + ACM (지금은 `*.cloudfront.net`)
- NAT Gateway (비용 때문에 public subnet + task public IP)
- 최소 IAM / 비용 알람 / RDS 중지 스케줄
- 프론트 CDN 분리 캐시(`/assets`만 캐시) — 현재 CachingDisabled

---

## 10) 세션 로그 (최신 위)

### 2026-07-25 — 따종/고덕 공유 가져오기
- `share_import.py` + UI `ShareImport`: 고덕 단축링크는 좌표 자동, 따종은 텍스트 파싱 후 지도 탭
- Nominatim은 중국 주소에 거의 실패 → 따종 좌표 자동은 보류

### 2026-07-25 — 설명 URL 링크화
- 마커/구역 설명 보기에서 URL을 클릭 가능한 링크로 표시 (`frontend/src/linkify.tsx`)
- 고덕/바이두: 웹 스크랩 비추천, 공식 API는 Key 발급 되면 검색 보조로 가능(미연동)

### 2026-07-21 — HTTPS + 히스토리 문서화
- CloudFront 배포, ALB SG를 CloudFront only로 제한
- `DEV_HISTORY.md` 및 `.cursor/rules/dev-history.mdc` 추가
- 앱 URL: https://d232kzujcg4ufp.cloudfront.net

### 2026-07-21 — AWS 원격 배포
- GitHub Actions OIDC 수정 후 ECS 배포 성공
- 테스트 계정으로 로그인·health 확인

---

## 11) 에이전트용 체크리스트 (새 환경)

1. `git clone https://github.com/juranikr/cloudmiddle.git` → 이 파일 읽기
2. 로컬: README대로 backend/frontend 실행
3. AWS 작업: CLI 로그인 + `infra/backend.hcl`·`terraform.tfvars` 복원(예제는 `*.example`)
4. 배포: `main` push 또는 Actions `workflow_dispatch`
5. 작업 끝나면 **이 파일 갱신 + commit + push**
