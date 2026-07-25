# cloudmiddle / 지난 여행 지도 — 개발 히스토리 (Living Doc)

> **이 파일은 프로젝트의 단일 컨텍스트 소스입니다.**  
> Cursor 에이전트는 작업 시작 전 반드시 읽고, 요청·수정이 끝날 때마다 갱신한 뒤 GitHub `main`에 push 합니다.  
> 규칙: `.cursor/rules/dev-history.mdc`

최종 갱신: 2026-07-26 (KST) — 에이전트 지식베이스·자동읽음·웹추천·즐겨찾기

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
| RDS | `tourmiddle-dev-postgres…` (Postgres 16, `db.t4g.micro`, **publicly accessible**, SG `0.0.0.0/0:5432` — 강한 비밀번호 의존) |
| GitHub OIDC role | `arn:aws:iam::155557574983:role/tourmiddle-dev-github-actions` |
| TF state | S3 `tourmiddle-tfstate-155557574983` + DynamoDB `tourmiddle-tf-lock` |
| App secret | Secrets Manager `tourmiddle-dev/app` (`DATABASE_URL`, `JWT_SECRET`, `SEED_PASSWORD_*`, `GROQ_API_KEY`, `GROQ_MODEL`) |
| 이미지 S3 | `tourmiddle-dev-place-images-155557574983` |
| 이미지 CDN | https://d3qw5zq6yb15c.cloudfront.net |
| 에이전트 스케줄 | EventBridge `cron(0 18 * * ? *)` = 매일 03:00 KST → ECS task `tourmiddle-dev-agent` |

**트래픽 경로:** 브라우저 → CloudFront(HTTPS) → ALB(HTTP, CloudFront prefix만 허용) → ECS Fargate → RDS

**대략 월 비용 (24/7):** 약 $45–60 (ALB+RDS+Fargate 중심, NAT 없음). CloudFront는 소량 트래픽이면 소액 추가.

### 운영 접속·계정

| 용도 | 값 |
|------|-----|
| 앱 URL | https://d232kzujcg4ufp.cloudfront.net |
| 성주한 | `joohan92@naver.com` (비밀번호: Secrets `SEED_PASSWORD_JOOHAN`, Git에 평문 없음) |
| 국서정 | `tjwjd629@naver.com` (비밀번호: Secrets `SEED_PASSWORD_GUKSEO`) |
| 테스트 | `test@test.com` / `test1234` |
| 로그인 UI | 테스트 계정 안내 문구 없음 (직접 입력) |

> 레포가 **public**이라 실사용 비밀번호는 README/DEV_HISTORY/seed 코드에 넣지 않음. ECS 기동 시 `seed.py`가 Secrets 환경변수로 해시 갱신.

---

## 3) 제품·기능

- 지도: Leaflet + OSM, 중심 지난
- 마커: point(핀) / polygon(구역), 카테고리: tourist, lodging, restaurant, transport, shopping, drink, convenience, other
- UX: 핀 모드 → 드래프트 + ConfirmBar(입력/취소); 구역 모드 → 탭 선택(점-in-폴리곤), 드래그로 그리기; 핀 모드에서는 구역이 클릭을 가로채지 않음
- 장소는 **공유 모델**: 단일 소유자 없음, `place_contributors` + 로그인 사용자 전원 수정/삭제. **「내 마커」필터 제거**
- 이력: `place_events` (create/update/delete/merge/image_*/context_*/agent_create/**rollback**) + `groq_read_at`
- **Groq ReAct+tools** (`backend/app/agent/`): 미읽음 이벤트·이의신청·롤백 기반 병합·컨텍스트·웹검색(DDG)·장소 추가·이미지 순서. 수동 `POST /api/admin/agent/run`, 매일 새벽 자동
  - 병합/추가 시 관련 사용자에게 **인앱 메시지** (`user_messages`)
  - **이의신청** (`place_appeals`) → 다음 주기에 `list_open_appeals`로 재고려
  - 편집은 덮어쓰기보다 **기존 기록 보존·보완** (append_note, local_name 병기)
  - UI/설명은 한국어, **명칭·주소는 현지 표기 유지**
  - 에이전트 변경은 `before` 스냅샷 저장 → 관리자 롤백 가능
  - `list_recent_rollbacks`: 롤백 교훈을 읽고 **같은 방향 수정 반복 금지**
- **메시지함** UI (상단) + 장소 상세/메시지에서 이의신청
- **관리자** `/admin` (성주한 `joohan92@naver.com`만, `ADMIN_EMAILS`): 에이전트 수동 실행, **에이전트 변경 이력·롤백**, 사용자 CRUD, 미읽음/Groq 상태. API 키는 Secrets Manager
- 기존 마커에 `place_events`가 없으면 기동 시 **create 미읽음 백필**
- **이미지**: S3 presigned PUT + CloudFront URL, 상세 상단 슬라이드 (`ImageSlideshow`)
- 주소 검색: 백엔드 `/api/geocode` → Nominatim
- 위치: HTTPS/localhost에서 GPS; HTTP LAN(아이폰)은 지도 중심 **가상 위치**
- 지도 뷰: center/zoom·locate 플래그 `localStorage` 유지
- 마커 설명: `http(s)://`·`www.` URL 자동 링크 (보기 모드, 새 탭)
- **공유 초안**
  - **메인**: `고덕 공유하기로 초안만들기` → surl에서 명칭·주소·좌표(GCJ→WGS84) → 등록 폼
  - **등록 화면(핀 찍은 뒤)**: `따종 공유하기로 초안만들기` → 이름/설명/링크만 채움(위치는 이미 찍은 좌표 유지)
  - 고덕 Key 없음(중국 번호) → 따종 주소 자동 지오코딩 불가, Nominatim도 중국 주소 실패
- 인증: JWT. 운영 계정은 §2 표 참고. 테스트만 `test@test.com` / `test1234`

---

## 4) 스택·디렉터리

```
cloudmiddle/
  DEV_HISTORY.md          ← 이 파일 (컨텍스트 소스)
  .cursor/rules/          ← Cursor 강제 규칙
  Dockerfile              ← multi-stage: FE build + FastAPI가 static 서빙
  backend/app/            ← FastAPI (main, auth, models, agent/, storage, events, …)
  frontend/src/           ← React/Vite (MapPage, MarkerPanel, ImageSlideshow, …)
  infra/                  ← Terraform (+ S3 images CDN, EventBridge agent)
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
- RDS 비밀번호·JWT·Groq·시드 비번은 Secrets Manager (`tourmiddle-dev/app`)
- Groq 키를 채팅에 붙여 넣었음 → **콘솔에서 로테이션 권장** (레포 public)
- Terraform `secret_version`은 `lifecycle.ignore_changes`로 CLI 추가 키를 덮어쓰지 않음

---

## 9) 의도적으로 안 한 것 / 다음 후보

- 커스텀 도메인 + ACM (지금은 `*.cloudfront.net`)
- NAT Gateway (비용 때문에 public subnet + task public IP)
- 최소 IAM / 비용 알람 / RDS 중지 스케줄
- 프론트 CDN 분리 캐시(`/assets`만 캐시) — 현재 CachingDisabled
- 에이전트 변경 승인 큐(현재 자동 merge/create)
- 이미지 용량·개수 하드 리밋 / 클라이언트 리사이즈
- 중국 웹(따종/고덕) 공식 검색 API (Key·번호 이슈)

---

## 10) 세션 로그 (최신 위)

### 2026-07-26 — 지식베이스·즐겨찾기·웹 추천
- 에이전트 actor 이벤트는 groq_read_at 즉시 기록(미읽음에서 제외)
- gent_knowledge + tools list_knowledge/upsert_knowledge/geocode_place
- 미읽음 0이어도 연구 사이클로 web_search→create_place
- 사용자 즐겨찾기 API/UI, 관리자 지식베이스 패널

### 2026-07-26 — Groq 모델 차단 대응
- 원인: llama-3.3-70b-versatile 가 Groq 프로젝트 limits에서 차단(403)
- 기본/Secrets 모델 → openai/gpt-oss-120b
- run_agent 예외를 500 대신 관리자 UI용 메시지로 반환

### 2026-07-26 — 관리자 에이전트 롤백
- `PlaceEventAction.rollback` + `backend/app/rollback.py` (merge/update/context/agent_create/image_reorder)
- 에이전트 조치에 `before` 스냅샷 저장; tool `list_recent_rollbacks`
- 관리 API: `GET /api/admin/agent/actions`, `POST .../rollback`
- `/admin`에 에이전트 변경 이력·롤백 버튼; 롤백은 미읽음으로 남아 다음 주기 교훈

### 2026-07-25 — 관리자 배포 수정 + 장소 이력
- FastAPI 204 DELETE `response_class=Response`로 기동 실패 수정
- `GET /api/markers/{id}/events` + 상세 패널「변경 이력」

### 2026-07-25 — 관리자 페이지 + 이력 백필
- `/admin`: 에이전트 수동 실행, 사용자 추가/비번/삭제 (관리자 이메일만)
- `ensure_schema`가 이벤트 없는 기존 마커에 미읽음 create 이력 백필

### 2026-07-25 — 에이전트 알림·이의신청·보존 정책
- `user_messages` / `place_appeals` + API(`/api/messages*`, `/api/appeals`)
- 병합→기여자 알림, 추천 추가→전원 알림, 이의는 다음 새벽 재검토
- 에이전트: append 위주 편집, 한국어 안내 + 현지 명칭 병기

### 2026-07-25 — Groq 에이전트 + S3 이미지 + 공유 장소
- 스키마: `place_events`, `place_contributors`, `place_images`, Marker에 `agent_context`/`merged_into_id`/`is_agent_suggested`
- FE: 내 마커 제거, 전원 수정, 상세 슬라이드 업로드
- infra: S3+이미지 CloudFront, ECS에 GROQ/S3 env, EventBridge 매일 03:00 KST 에이전트
- 수동 실행: 로그인 후 `POST /api/agent/run` 또는 ECS `python -m app.agent`

### 2026-07-25 — 운영 계정 교체
- alice→성주한(`joohan92@naver.com`), bob→국서정(`tjwjd629@naver.com`), carol→테스트(`test@test.com`)
- 비밀번호는 Secrets Manager + ECS env. 로그인 페이지 테스트 안내 삭제
- 기존 user_id 유지(별칭 이메일로 찾아 갱신) → 마커 소유권 유지

### 2026-07-25 — 공유 초안 UI 분리
- 메인: 고덕 초안 버튼 / 등록 패널: 따종 초안 버튼
- 검증용 고덕 예시(桥下把子肉 / surl hRQqfZY1n6gg): GCJ 36.675519,117.099684 → WGS84 36.675065,117.093552

### 2026-07-25 — 따종/고덕 공유 가져오기
- `share_import.py` + UI: 고덕 단축링크 좌표 자동, 따종은 텍스트 파싱(Key 없어 주소→좌표 API 불가)

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
