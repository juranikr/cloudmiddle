# cloudmiddle / WONRAE 遠來 — 개발 히스토리 (Living Doc)

> **이 파일은 프로젝트의 단일 컨텍스트 소스입니다.**  
> Cursor 에이전트는 작업 시작 전 반드시 읽고, 요청·수정이 끝날 때마다 갱신한 뒤 GitHub `main`에 push 합니다.  
> 규칙: `.cursor/rules/dev-history.mdc`

최종 갱신: 2026-08-23 (KST) — 삭제 후에도 이어지는 도시별 이력·결정론적 큐 학습 보강

---

## 1) 한 줄 요약

한국 여행자를 위한 **중국 도시별 공유 여행 지도** 웹앱. 지난(济南)과 선양(沈阳)을 지원.
핀/구역 마커, JWT 로그인, 로컬 SQLite·Docker Postgres / AWS Postgres, CloudFront HTTPS로 원격 서비스 중.

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
| App secret | Secrets Manager `tourmiddle-dev/app` (`DATABASE_URL`, `JWT_SECRET`, `SEED_PASSWORD_*`, `GROQ_API_KEY`, `GROQ_MODEL`, `ARCGIS_API_KEY`, `BRAVE_SEARCH_API_KEY`) |
| 이미지 S3 | `tourmiddle-dev-place-images-155557574983` |
| 이미지 CDN | https://d3qw5zq6yb15c.cloudfront.net |
| 에이전트 스케줄 | EventBridge `cron(0 2,10,18 * * ? *)` = 매일 11:00/19:00/03:00 KST → ECS task `tourmiddle-dev-agent` |

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

- 지도: Leaflet + OSM, 도시 선택에 따라 중심·검색·GPS 유효 범위 전환
- 가까운 point 마커는 현재 줌의 화면 거리 기준으로 숫자 클러스터에 묶고, 클릭 시 카테고리·구역을 보며 장소를 선택
- 마커: point(핀) / polygon(구역), 카테고리: tourist, lodging, restaurant, transport, shopping, drink, convenience, other
- UX: 핀 모드 → 드래프트 + ConfirmBar(입력/취소); 구역 모드 → 탭 선택(점-in-폴리곤), 드래그로 그리기; 핀 모드에서는 구역이 클릭을 가로채지 않음
- 장소는 **공유 모델**: 단일 소유자 없음, `place_contributors` + 로그인 사용자 전원 수정/삭제. **「내 마커」필터 제거**
- 이력: `place_events` (create/update/delete/merge/image_*/context_*/agent_create/**rollback**) + `groq_read_at`
- **Groq ReAct+tools** (`backend/app/agent/`): 도시별 미읽음 이벤트를 우선 처리하고, 실제 DB 변화·새 근거·측정 가능한 성과 공백을 기준으로 조사 전략을 조정. 수동 `POST /api/admin/agent/run`, 매일 3회 활성 도시별 실행
  - `city_id`를 러너와 모든 장소 도구에 강제 전달하고 `place_events.city_id`를 장소와 별도로 영구 보존해, 장소가 삭제되어도 제남/선양 큐·장소·지식·검색 이력이 섞이거나 사라지지 않음
  - 자동 생성·병합 비활성 상태에서도 `agent_proposals`에 근거·출처 URL·신뢰도를 저장하고 `/admin`에서 승인/거절
  - 도시/장소 지식 topic을 `city:{id}:...` / `place:{id}:...`로 분리해 같은 `research_strategy`가 덮어써지지 않음
  - `place_insights`로 위치 맥락·역사·방문 정보·현지 팁을 출처·확인일·신뢰도와 함께 구조화
  - 병합/추가 시 관련 사용자에게 **인앱 메시지** (`user_messages`)
  - **이의신청** (`place_appeals`) → 다음 주기에 `list_open_appeals`로 재고려
  - 편집은 덮어쓰기보다 **기존 기록 보존·보완** (append_note, local_name 병기)
  - UI/설명은 한국어, **명칭·주소는 현지 표기 유지**
  - 에이전트 변경은 `before` 스냅샷 저장 → 관리자 롤백 가능
  - `list_recent_rollbacks`: 롤백 교훈을 읽고 **같은 방향 수정 반복 금지**
  - 실행별 실제 DB 변화·새 근거·반복 호출·성과 점수·남은 공백을 `agent_runs`/`agent_run_steps`에 기록하고 관리자 UI에서 단계별 확인
  - 시스템 교정·관리자 롤백·동일한 영속 ID 또는 제목+위치 스냅샷으로 같은 실체임이 입증된 생성→삭제 이력은 모델이 재검색하지 않고 검증된 `AgentLesson`/`AgentKnowledge`로 승격한 뒤 읽음 처리하며, 일반 사용자 요청은 그대로 모델 큐에 보존
  - 동일 조사 호출 차단, 새 근거 없는 행동 감지, 측정 가능한 후속 과제와 자동 공백 해소로 고정 스텝 대신 성과 기반 종료
  - 신규 장소 발굴은 별도 `candidate_discovery` 미션으로 주기적 시간을 보장하고, 실제 검색·본문/좌표 검증 뒤 `agent_proposals`를 만든 경우에만 완료
  - 사진·구역처럼 현재 해결 불가능한 결손은 `agent_quality_gap_dispositions`에 근거·조건 지문·냉각 시간을 남겨 같은 실패를 매 배치 반복하지 않고 조건이 바뀌면 자동 재개
  - Brave Place는 배치의 일시적 발견 단서로만 사용하며 응답 원문·ID·건수·후속 질의·모델 자유서술을 DB/실행 이력/로그에 보존하지 않음. 일반 대화에는 Brave를 노출하지 않고 독립 출처로 재검증된 canonical 값만 제안 가능
- **메시지함** UI (상단) + 장소 상세/메시지에서 이의신청
- **관리자** `/admin` (성주한 `joohan92@naver.com`만, `ADMIN_EMAILS`): 에이전트 수동 실행, 실행별 DB 증감·실제 변경·전체 도구 과정, **에이전트 변경 이력·롤백**, 사용자 CRUD, 미읽음/Groq 상태. API 키는 Secrets Manager
- 기존 마커에 `place_events`가 없으면 기동 시 **create 미읽음 백필**
- **이미지**: S3 presigned PUT + CloudFront URL, 상세 상단 슬라이드 (`ImageSlideshow`)
- 주소·장소 검색: 백엔드 `/api/geocode`가 **운영 DB → ArcGIS → Nominatim → Wikidata** 후보를 도시 경계 안에서 병렬 조회·근접 병합
  - 이미 등록된 장소를 최우선 노출하고 상세로 바로 이동해 중복 방지
  - 출처, 일치도, 교차 확인, Wikidata ID를 결과에 포함
  - ArcGIS API 키가 없으면 단독 결과는 참고용(`storage_allowed=false`)이며 지도 직접 지정 후 등록
  - Nominatim 공개 정책에 맞춰 1 req/s 제한과 12시간 캐시 적용
- 위치: HTTPS/localhost에서 GPS; HTTP LAN(아이폰)은 지도 중심 **가상 위치**
- 좌표 출처: 각 마커에 provider/external ID/query/source URL/confidence/CRS/검증일을 보존
- 따종·고덕 공유 초안도 선택 도시의 경계·검색 컨텍스트를 사용(제남 하드코딩 제거)
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

- 로컬 통합 권장: Docker Compose의 PostgreSQL `cloudmiddle_local` + 실제 배포 이미지 (`127.0.0.1:18000`)
- 경량 개발 대안: SQLite `backend/jinan_travel.db` (Docker 없이 사용 가능)
- 프로덕션: Postgres (Secrets Manager의 `DATABASE_URL`)
- FE API base: 배포 시 같은 오리진 `""` (`frontend/src/api.ts`)

---

## 5) 로컬 개발 (다른 PC에서)

배포 전 전체 통합 검증은 저장소 루트에서 아래 한 명령으로 실행합니다.

```powershell
.\dev\predeploy.ps1
```

이 명령은 백엔드 전체 테스트, 프런트 빌드, Python 컴파일, diff/Compose 검사,
로컬 Docker 앱·DB 기동, `db_mode=local`, 테스트 로그인과 도시 API까지 확인합니다.
운영 스냅샷 복제·정제와 운영 DB 읽기 전용 진단은 루트 `README.md`의
`clone-production-db.ps1` / `production-readonly.ps1` 절차를 따릅니다.

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

### 2026-08-23 — 삭제된 장소 이력 복구 + 정정 이력의 결정론적 학습
- 운영 수동 run #136·#137과 정기 run #139를 추적한 결과, 선양의 시스템 정정 3건을 모델이 새 수정 요청으로 오해해 매번 19~24라운드 검색하면서도 `Partial`, 미읽음 `3→3`, 실제 변경 0건으로 끝나는 문제를 재현
- `place_events.city_id` nullable FK/index를 추가하고 이벤트 생성 시 도시를 장소 ID와 별도로 보존. 기존 데이터는 현재 마커, 명시적 payload 도시/마커/이벤트 참조, 서로 겹치지 않는 도시 viewbox 좌표만으로 고정점 백필하며, 사용자·제목 같은 모호한 정황은 추론에 쓰지 않고 `NULL` 격리함
- 장소 삭제로 `place_id=NULL`이 된 이력도 도시가 확인되면 원래 큐에서 조회·읽음 처리할 수 있게 변경. PostgreSQL insert trigger가 구버전 writer의 살아 있는 `place_id`를 보완하고, 도시를 끝내 확인할 수 없는 신규 이력은 거부하며 기존 미귀속 이력은 관리자 경고로 노출함
- `actor=system + cleanup_version` 교정/아카이브, 명시적 관리자 롤백, 동일한 영속 장소/상관 ID 또는 양쪽의 같은 제목+좌표/주소 스냅샷으로 실체가 입증된 생성→삭제 순변화 0 쌍만 서버가 결정론적으로 처리. 제목·시간만 같은 체인점 이력과 일반 사용자 요청은 자동 승인하지 않음
- 교정 원칙은 `AgentLesson(status=validated)`와 구조화 `AgentKnowledge`로 승격해 다음 배치가 실제로 검색·행동 정책에 재사용하고, 모델 호출 없는 처리도 `AgentRun(outcome=queue_acknowledged)`과 `이력 학습 · 큐 정리` 관리자 배지로 감사 가능하게 기록
- 큐 승인·지식 승격·`AgentRun` 생성은 한 트랜잭션으로 묶고, 일부만 전처리된 상태에서 모델을 실행할 수 없으면 전부 롤백해 이력만 사라지는 간극을 제거. Step Functions 다중 도시 완료 결과도 도시별 캐시로 분리해 다른 도시 결과가 표시되지 않게 함
- 실행 행 커밋 뒤 Groq/개인화/지식 검색/프롬프트 준비에서 예외가 나도 같은 `AgentRun`을 `failed`·종료시각·실패 단계/유형으로 닫아 영구 `running` 감사 이력이 남지 않게 함
- 로컬 PostgreSQL 16에서 FK/고정점 마이그레이션과 insert trigger를 실측: 살아 있는 마커의 구버전 insert는 도시 자동 보완, 장소·도시가 모두 없는 신규 insert는 SQLSTATE `23502`, 기존 미귀속 레거시는 격리 유지
- 검증: 백엔드 363개 테스트, 프런트 production build, Python compileall, Docker PostgreSQL 마이그레이션·컨테이너 health, 로컬 API health/login/cities를 포함한 `dev/predeploy.ps1` 전체 통과
- 운영 배포: commit `0596a21`, GitHub Actions `32639692558` 성공, ECS task definition `tourmiddle-dev-api:6` steady state 및 `/api/health` 정상
- 운영 데이터 정리: 잘못 배치 후 삭제된 만신호텔 이력 #360·#361을 정식 장소 #110에 연결해 읽음 폐기하고 교정 #440 기록. 장소 #96의 실제 description에서 근거 없는 정확 주소·일률 영업시간을 제거하고 교정 #441 기록
- 연속 실행 실측: 선양 run #142가 기존 교정 #436~#438과 신규 감사 #440~#441을 모델 호출 없이 `5→0`, `completed`, `queue_acknowledged`, score 40으로 승격; 지난 run #143도 관리자 롤백 #439를 `1→0`, score 8로 학습
- 이어진 선양 run #144는 빈 큐에서 품질 미션을 재개해 `zone_catalog_disposition`, `completed`, score 4와 재시도 커서를 남겼고, run #145는 모든 후보 프런티어의 12시간 냉각을 확인해 모델 호출 없이 `deferred`로 종료. 운영 미읽음 이력/열린 이의 0건, 영구 `running` run 0건, 교정 lesson/knowledge 각 5건 생성 확인

### 2026-08-23 — 수동 배치 실행 격리 + 감사 가능한 성과·연속성
- 관리자 수동 실행도 API 컨테이너 안의 background thread가 아니라 예약 배치와 같은 Step Functions/Fargate 경로로 통합하고, 도시별 실패 격리·AWS 실행 재연결·정확한 도시 결과 폴링을 추가
- PostgreSQL 도시별 advisory lock으로 수동/예약/중복 클릭이 겹쳐도 같은 도시 에이전트는 하나만 실행하며, 중복 요청은 DB를 변경하지 않는 `already_running` 성과로 기록
- `completed` 상태와 실제 여행자 가시 변경을 분리해 `traveler_visible_changed`, `proposal_created`, `verified_or_waived_no_change`, `deferred_or_blocked`, `no_yield`, `failed`로 관리자 화면에 표시하고 다음 대상·커서·행동도 함께 노출
- 후보 미션은 여행 역할을 끝까지 고정하고 12시간 냉각 후 재개. 실패한 본문 URL은 실패 출처로 이관한 뒤 같은 후보의 다음 미열람 URL 또는 실패 호스트를 제외한 정확 검색으로 전환
- 신규 장소/insight는 현재 실행에서 직접 읽은 대상 일치 본문만 근거로 사용하고, 구체 장소명·60자 이상 설명·역할·구조화 insight를 서버에서 검증. 잘못된 지점/주소·일반 라벨·Brave 단독 좌표는 제안 불가
- 이미지 감사 규칙을 v2로 올려 과거 잘못된 `source_exhausted`를 재평가하고, 장소 불일치 사진은 일시 차단 후 다른 후보를 찾되 정확한 영문 별칭은 이미지 검색·첨부에만 제한적으로 허용

### 2026-08-23 — 다중 검색 발견 레인 + 연속성 있는 품질 종료
- Brave Search Place Search를 기존 Yahoo/Yandex 웹 검색·ArcGIS/Nominatim/Wikidata 위치 검색 앞단의 **발견 단서**로 추가하고, 국가·도시별 언어/별칭/공식 도메인 프로필로 중국 외 도시도 같은 구조를 사용하도록 일반화
- 표준 Search 플랜의 비보존 조건에 맞춰 Brave 후보는 실행 메모리에서만 사용: chat 경로 차단, ephemeral ID·원문·파생 count/hash·후속 query·오류 자유서술을 Step/checkpoint/task/summary/SearchLog/CloudWatch에 저장하지 않음
- 저장 가능한 비-Brave 지오코더 또는 직접 읽은 공개 본문으로 같은 장소를 독립 확인한 필드만 canonical 제안으로 승격하고, 좌표 저장 허용 플래그 누락은 fail-closed 처리
- 신규 장소 발굴을 품질 보강과 분리한 가중 레인으로 추가. 차단 시 같은 task/mission/work item/checkpoint를 이어 받고, 검색 없이 차단 선언하거나 모델이 완료를 자칭하는 경로는 서버가 거절
- 사진 검색 3회는 `정상 무후보 / 공급자 오류 / 후보 있음`으로 구분하고, 정상 무후보 3회만 `source_exhausted`; 오류는 냉각 `blocked`. 구역은 전체 polygon geometry가 유효하고 어느 구역에도 포함되지 않을 때만 `waived`
- 관리자 실행 이력에 검색→독립 본문→저장 가능한 좌표→승인 제안 퍼널과 조건부 보류 결손 수를 표시하되 Brave 응답 파생 건수는 표시·저장하지 않음
- 키는 코드/문서가 아니라 AWS Secrets Manager에만 저장하고 `BRAVE_SEARCH_STORAGE_RIGHTS=false`로 배포

### 2026-08-11 — 도시별 근거 기반 에이전트·구조화 장소 지식
- 러너/툴/스케줄을 활성 도시별로 실행하고 모든 장소·이벤트·이의·재검증 쿼리에 `city_id` 경계 적용
- 관리자에서 도시 선택 + 웹 조사 모드 실행, `agent_proposals` 신규 장소/병합 승인·거절 UI
- 신규 제안은 evidence/source URLs/confidence와 구조화 insight 2건 이상 필수
- `place_insights`: location/history/visit/tip을 타임라인형 장소 상세에 표시
- 마커 좌표 provenance(provider/external ID/query/source URL/confidence/CRS) 저장 및 화면 노출
- 따종/고덕 공유 가져오기와 GPS 범위를 선택 도시 컨텍스트로 전환
- 고덕 공유문 `5.0分` 평점 줄/공유 문구를 제목으로 오인하던 문제 수정, 실제 POI·호텔명을 우선 선택
- 선양 초기 조사 축과 공통 편집 기준을 지식베이스 seed로 추가
- 운영 선양 큐 2건 실측에서 16스텝 조기 소진을 확인해, 큐 실행 예산을 `min(140, 48+4n)`으로 확대
- 에이전트 동일 제목/설명/메모/카테고리 재호출은 이벤트를 만들지 않도록 idempotent 처리

### 2026-08-11 — 도시별 다중 위치 검색
- `/api/geocode`: 운영 DB·ArcGIS·Nominatim·Wikidata 병렬 조회, 도시 viewbox 필터, 140m 근접 후보 병합
- 검색 응답: `sources`, `confidence`, `confidence_label`, `storage_allowed`, `existing_marker_id`, `external_id`
- UX: 출처 배지·교차 확인 표시, 기존 장소 바로 열기, 검색 결과에서 등록 폼 이름·설명 자동 입력
- ArcGIS 익명 결과는 참고용으로 제한하고 지도 직접 지정 흐름 제공; 키 설정 시 `forStorage=true`
- 도시 검색 문맥 DB 필드 `cities.search_context` 추가(지난=산둥성, 선양=랴오닝성)
- 에이전트 `geocode_place`에 `city_id`를 추가해 같은 다중 검색기를 사용

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

### 2026-08-23 — Brave 다중 검색 운영 배포 + 5회 연속성 검증
- `7a37676`으로 도시별 검색 프로필·Brave 발견 전용 레인·품질 결손 disposition·관리자 발굴 퍼널을 배포하고, 운영 실측에서 발견한 중복 제안 fallback 성과 오판까지 `56d0b64`로 추가 보정
- 백엔드 285개 테스트, 프런트 production build, compileall/diff check, Docker PostgreSQL 로컬 health·로그인·도시 조회 스모크 통과
- Terraform은 무관한 외부 RDS SG 드리프트를 제외한 targeted plan(`2 add / 3 change / 2 destroy`)만 적용. API task definition `:5`, agent `:4`, Step Functions·IAM 연결 갱신; Actions `32621233807`, `32622527639` 모두 성공
- 운영 수동 Step Functions 5회 모두 `SUCCEEDED`; DB run #121~#129에 failed/running 잔류 0, mission run 전부 step별 checkpoint 보유
- 발견 레인 연속성 실측: 지난 #121→#127은 mission #23/work #111, 선양 #122→#128은 mission #24/work #112를 그대로 재개. 사이에 두 번의 품질 레인을 공정하게 실행한 뒤 보류 체크포인트로 복귀
- 운영 성과: 승인 대기 신규 제안 #32 `民生大街 (민생대가)` 1건(기존 marker 의미 중복 0), 장소 insight 4건, context update 이벤트 6건, 선양 구역 결손 waiver 1건. 직접 신규 marker는 승인 전이므로 0건
- 보존·안정성 감사: Brave 구조 필드/후보 ID·파생 수치의 step/checkpoint/task/run/proposal/CloudWatch 누출 0, Brave 좌표 제안 0, pending proposal-key 중복 0, 에이전트 403·`output_parse_failed`·Traceback·run failure 0
- 운영에서 제안 #32 생성 직후 `create_place` fallback이 같은 제안을 재사용하면서 성과를 2건으로 세던 흔적을 확인. 모든 `proposal_created=false`를 도구명과 무관하게 무성과로 처리하고 회귀 테스트 후 재배포

### 2026-08-14 — 운영 복제 기반 로컬 통합 테스트 + 에이전트 실행 모드 격리
- Docker Compose를 `127.0.0.1:18000` 앱 + `127.0.0.1:55432/cloudmiddle_local` PostgreSQL로 구성하고 화면/API에 `LOCAL INTEGRATION · DB local` 표식 추가
- `APP_DB_MODE=local|production_readonly` 안전 경계 추가: 로컬 DB 이름·호스트·포트 검증, 운영 진단은 libpq/transaction read-only + 로그인 외 HTTP 쓰기 503 + startup migration/seed 생략
- 운영 DB는 source read-only preflight 후 Docker `pg_dump` → staging restore → 이메일/비밀번호 정제 → 검증 → 원자 교체하며, 기본 모드는 채팅·메모·일정·이의·에이전트 trace 등 private content 제거
- `-RetainPrivateContent`는 명시적 장애 진단에만 사용하고, 다음 safe clone은 이전 private backup까지 제거; Windows 일일 예약 등록/해제 wrapper 제공
- `dev/predeploy.ps1`로 백엔드 전체 테스트·프런트 빌드·compileall·diff·Compose·로컬 health/header/login/cities를 한 번에 검증
- 사용자 큐와 자율 연구/DI 미션을 서로 다른 run으로 분리: 큐 run은 미션 attempts/checkpoint를 건드리지 않고, 큐가 끝난 다음 invocation에서 기존 미션을 재개
- DI는 활성 place의 exact `get_place`만 허용하고 다른 place/list 도구를 차단하며, exact target checkpoint를 포함한 서버 소유 evidence ref가 없으면 종료 불가
- 로컬 운영 복제본 실측: queue run #57은 #83 이의만 처리하며 DI #37/#105 불변, research run #60은 #105 감사 task/mission/work item을 원자적으로 완료하고 #103~105 marker 무변경
- 로컬 실측 중 발견한 Groq 400 두 종류를 배포 전에 수정: corrective 단계의 `get_place` 프롬프트/도구 모순, 구형 success metric의 추가 필드가 새 structured schema에 섞이던 문제
- 최종 로컬 기준 백엔드 243개 테스트, DI 집중 90개, 프런트 production build, Docker/UI/API 실측 통과
- 운영 배포 `2bf5c8b` / Actions run `31721345506` 성공, ECS rollout/health 정상
- 운영 연속 실행 검증: run #57은 queue mode로 사용자 큐 2→0 처리하면서 DI task #37 attempts=1·mission #11 last_run=56 유지; run #58은 #105 corrective 미션을 재개해 첫 근거 형식 오류를 같은 실행에서 교정하고 task #37·mission #11·work item #51 완료
- 운영 run #58 전후 marker #103·#104·#105의 `updated_at` 불변, `marker_changes=0`; `partial` 표시는 감사 실패가 아니라 도시 전체 사진·정보 품질 공백이 남았다는 뜻

### 2026-08-11 — 로컬 기준선 복구 + 지난/선양 분리 + 에이전트 안전 모드
- 원인 확인: Cursor가 `%TEMP%` 임시 clone에서 32개 커밋을 push해 원래 `cloudmiddle` 로컬만 뒤처짐
- 로컬 `main`을 `origin/main` `ca4a620`으로 fast-forward; 미추적 `backend/scripts/` 보존
- `cities` 테이블과 `markers.city_id` 추가; 기존 운영 장소는 지난(id=1)으로 자동 백필
- 지난·선양 도시 API, 도시 선택 UI, 도시별 장소 목록·지도 중심·검색 viewbox 적용
- 에이전트 안전 기본값: 무작업 자율 조사 중단, 자동 장소 생성/병합 차단, 큐 스텝 상한 축소
- 검색 결과 URL 전체를 `agent_search_results`에 저장해 반복 검색의 가짜 `new_count` 수정
- `cycle_*` 지식을 `operations_lessons`로 통합하고 기본 append 누적 중단
- EventBridge 에이전트 스케줄 하루 3회 → KST 03:00 하루 1회
- 검증: Python compileall, 로컬 SQLite 레거시 마이그레이션, 실제 FastAPI 도시/장소 API, 프런트 프로덕션 빌드 성공

### 2026-07-27 — 사이클당 작업량 대폭 확대
- 스텝 한도: 연구 사이클 45 → 110, 큐 사이클 상한 56 → 140 (40 + 건당 4)
- 사이클당 할당량 상향: fetch_page 2~4 → 4~8, create_place 1~5 → 5~12,
  재검증 3~5 → 8~12곳, 사진 보강 1~2 → 3~6곳
- 큐 사이클에도 여유 스텝 시 재검증·사진 보강 수행 항목 추가
- 조기 종료 방지 넛지 추가: 스텝 60% 미만 사용 시 최대 3회 잔여 할당량 수행 지시
  (첫 검증 실행에서 110스텝 중 26스텝만 쓰고 "다음 사이클 예고"로 끝내는 문제 확인)

### 2026-07-27 — 에이전트 추론 강도 high 적용
- gpt-oss 계열 모델 사용 시 reasoning_effort="high" 전달 (기존 기본값 medium)
- 병합 판단 등 미묘한 결정 품질 향상 목적, 비용은 출력 토큰 소폭 증가 수준
- 다른 모델(GROQ_MODEL 교체 시)에는 옵션을 넘기지 않아 호환성 유지

### 2026-07-26 — 고덕 공유 가져오기 504 수정
- 증상: surl.amap.com 리다이렉트 추적이 ECS(AWS IP)에서 매달려 CloudFront 30초 초과 → 504
  (로컬 IP에서는 정상 — amap의 데이터센터 IP 차단/지연으로 추정)
- _follow_redirects에 총 12초 예산 + 홉당 6초 타임아웃 → 게이트웨이 전에 빨리 실패
- 실패 시 텍스트 폴백: 공유 본문의 제목·주소로 초안 구성, 지오코딩 시도, 안 되면 지도 탭 안내

### 2026-07-26 — 에이전트 작업량 확대
- EventBridge 스케줄 하루 1회 → 3회 (KST 03:00/11:00/19:00, cron 0 2,10,18 UTC)
  tf 수정 + aws events put-rule로 즉시 반영 (다음 terraform apply 시 드리프트 없음)
- 연구 사이클 스텝 한도 36 → 45

### 2026-07-26 — 모바일 GNB 더보기 상태 수정
- 더보기는 모달 토글일 뿐 탭 전환이 아니므로 mobileTab을 바꾸지 않게 변경
- 시트 닫으면 지도 탭 활성 상태 유지, 재탭 시 토글로 닫힘
- 메시지 화면에서 더보기 열면 mobileTab을 map으로 되돌려 이중 활성 방지

### 2026-07-26 — 병합 보수화 + undo_merge (과병합 사고 복구)
- 사고: 유연화된 병합 규칙이 과병합 유발 — 오룡담공원(#12)→표돌천(#27), 파자육 가게 3곳(#15/#21→#20)
  병합 후 사용자 이의도 기각. 관리자 롤백 API로 3건 복구 완료.
- 병합 정책 재균형: 기본값 '병합 안 함', 같은 실체 확신 + 웹 근거 필수.
  인접한 별개 명소(趵突泉/五龙潭/大明湖)·같은 음식명(把子肉) 다른 가게·다른 지점은 절대 금지.
- 이의 비대칭 해소: '다른 장소/다른 지점' 주장 = 강한 반증. 명백한 반증 없으면 수용,
  기각은 웹 근거를 agent_note에 제시할 때만.
- 새 툴 undo_merge: 에이전트가 스스로 잘못된 병합을 되돌림 (스냅샷·이미지 원복, 병합 이벤트에 rolled_back 마킹)
- 지식베이스 jinan_merge_lessons를 개정 정책으로 직접 UPDATE (오판/정당 사례 명시)

### 2026-07-26 — 언어 규칙 강제 (설명 한국어, 명칭 中韓 병기)
- create_place/update_place_fields에 하드 검증: 제목·append_note·설명에 한국어 없으면 거부(korean_required)
- 제목 형식 '中文名 (한국어 명칭)', 설명 본문 한국어, 주소는 지도 검색용으로 중국어 원문 유지
- update_place_fields에 replace_description 추가: agent 장소 또는 한국어 없는 설명만 전면 재작성 허용,
  사용자가 쓴 한국어 설명은 보호(user_content_protected)
- 연구 사이클에 언어 정비 단계 추가: 기존 중국어/영어 위주 장소를 규칙대로 재작성
- 운영 정비 완료: 에이전트 3회 실행으로 중국어 설명 전량 한국어화(百花洲·宽厚里 등),
  영문 브랜드 핀 4곳(MixC·Lixia·HeyTea·Qingguohui)은 API로 한국어 병기 직접 추가 → 위반 0건

### 2026-07-26 — 웹 스크래핑 조사 필수화 + 조사 이력 기록
- 새 테이블: agent_search_logs(검색어·시각·새 콘텐츠 수확량), agent_web_visits(열람 URL·횟수, unique)
- 새 툴: fetch_page(본문 스크래핑, stdlib HTMLParser, 방문 자동 기록, already_visited 표시),
  list_research_history(검색어별 집계 + 최근 열람 목록)
- web_search 개선: 결과마다 seen(기열람) 표시, 검색어 자동 로깅, past_searches 반환
- duckduckgo-search 7.5.5가 엉터리 결과 반환(deprecated) → ddgs 9.14.4로 교체
- 매 사이클 웹 조사 1회 필수(큐 처리 후): 이력 확인 → 덜 판/새 키워드 → 미열람 글 2~4개 정독 →
  반복 추천 미등록 장소 create_place, 기존 장소와 겹치는 유용 정보는 update_place_fields/context로 보완
- 조사 전략은 upsert_knowledge(topic 'research_strategy')에 축적, fetch_page 미사용 시 넛지
- steps_limit: research 36, 큐 사이클 18+unread*4(최대 56)
- 순서 조정: 웹 조사를 사진 보강보다 먼저, 전략 저장(upsert_knowledge)은 조사 직후 즉시
  (스텝 소진으로 지식 저장이 누락되던 실패 패턴 수정)
- 운영 검증: 泉城广场·百花洲·宽厚里 신규 등록, 黑虎泉 무료입장 정보 보완,
  research_strategy 지식에 소진/미개척 키워드 전략 축적 확인

### 2026-07-26 — 오래된 장소 재검증 로직
- markers.last_verified_at 컬럼 추가 (멱등 마이그레이션)
- 새 툴: list_stale_places(30일 이상 미확인, 오래된 순) / verify_place(valid|closed|moved|uncertain)
- moved 판정 전 지점(분점) 구분 재검토 강제: 다른 지점이면 좌표 유지 + note, 같은 지점 이전 확실할 때만 좌표 갱신
- closed는 삭제 금지, note는 agent_context에 자동 병합, 이벤트(context_update)로 이력 기록
- 연구 사이클에 재검증 단계(3~5곳) 추가, steps_limit 10→18
- 참고: 07-26 03:00 KST 자동 실행은 uq_place_user 중복 키 버그로 크래시 (같은 날 낮에 수정 배포됨)

### 2026-07-26 — 에이전트 웹 이미지 보강
- 새 툴: search_place_images(위키미디어 커먼즈, 자유 라이선스) / attach_image_from_url(S3 업로드)
- 제한: https만, jpeg/png/webp, 5MB 이하, 장소당 최대 8장, 출처·라이선스 event payload 기록
- list_places 등 brief에 image_count 추가 → 사진 없는 장소 탐색 가능
- 연구 사이클에 사진 보강 단계(1~3곳) 추가
- storage.put_object_bytes 추가 (ECS task role에 s3:PutObject 이미 있음)

### 2026-07-26 — 관리자 에이전트 실행 비동기화
- 증상: 사이클이 60초 넘으면 CloudFront 504 → UI에 "실패"로 표시 (실제는 성공)
- `/api/admin/agent/run` 백그라운드 스레드 실행 + `/agent/run/status` 폴링 (관리자 UI 3초 간격, 최대 10분)

### 2026-07-26 — 병합 판단 유연화
- 증상: 동일 명소(천불산 11 / 千佛山 25)인데 150m 고정 반경 미충족으로 병합 안 됨, 이의도 거리로 기각
- 프롬프트: "같은 실체인가" 기준 명시 — 동명(한글/한자/병음)·이의 주장·웹 근거면 거리 무관 병합, 넓은 명소는 radius 1000~5000m
- 연구 사이클에 전체 지도 중복 스캔 단계 추가
- 툴 설명: find_nearby_candidates/merge_places/list_places에 거리=참고 신호 명시
- 운영 KB의 "150m 규칙" 교훈을 직접 교정 (fix_kb 스크립트, agent_knowledge UPDATE)

### 2026-07-26 — 툴 인자 스키마 오류 내성
- 증상: 모델이 upsert_knowledge에 place_id:null → Groq 400 tool_use_failed로 사이클 중단
- upsert_knowledge.place_id 스키마 `["integer","null"]` 허용
- 러너: tool_use_failed 시 교정 지시 후 최대 3회 재시도 (사이클 유지)

### 2026-07-26 — PC 사이드바 스크롤 수정
- 증상: 왼쪽 패널 내용이 화면을 넘어가면 잘리고 스크롤 불가
- 원인: `.map-side .panel { flex: 1 }` + `overflow: auto`로 패널이 남은 공간에 축소 고정
- 수정: 패널 `flex: 0 0 auto; overflow: visible` → 내용만큼 늘어나고 `.map-side__scroll`이 스크롤 담당
- 검증: 관리자 에이전트 실행 ok=true (uq_place_user 500 재발 없음)

### 2026-07-26 — 기여 명단 vs 수정 이력
- `place_contributors`: 알림용 distinct 명단(유니크 유지). `place_events`: 반복 기여·수정 이력
- 에이전트 500 원인: merge 시 동일 (place,user) 중복 INSERT → ensure_contributor pending 가드 + set 병합
- runner 예외 시 `db.rollback()` 후 unread 집계
- update/agent 수정 payload에 before/after/changes 기록, API·MarkerPanel 이력에 필드별 diff 표시

### 2026-07-26 — 에이전트 작업 큐 전원 처리
- 원인: ReAct가 큐를 안 보고도 종료 가능, 넛지는 KB만, max_steps=12로 조기 종료, 프롬프트에 ID 목록 없음
- 시작 시 미읽음 이벤트·이의 ID를 유저 메시지에 주입
- 종료 시 unread>0이면 잔여 큐를 최대 4회 재주입해 계속 처리
- 스텝 예산: `min(48, 10+unread*4)`
- open 이의는 mark_appeals_read 거부 → resolve_appeal 필수
- unread 잔존 시 ok=false

### 2026-07-26 — 에이전트 지식필수·장소 쿼리
- ReAct 유지: upsert_knowledge 미호출 시 추가 턴으로 강제 유도
- list_places에 q/category/near_*/exclude_ids 필터
- find_nearby_candidates: 전체 활성 장소 비교임을 툴 설명에 명시

### 2026-07-26 — 구역 그리기 멀티터치 무시
- `ZoneDrawer`: 터치 2개 이상이면 스트로크 취소·무시 (핀치 줌이 구역으로 저장되지 않음)
- 한 손가락 탭 선택·드래그 그리기는 유지

### 2026-07-26 — 모바일 GNB 축소
- GNB: 지도 / 메시지 / 더보기
- 핀·구역·내위치는 상단 모드 토글, 즐겨찾기는 필터 칩만 유지

### 2026-07-26 — 반응형 지도 UI
- 모바일: 상단 검색+필터, 하단 GNB(지도/추가/즐겨찾기/메시지/더보기), 검색결과·상세 바텀시트
- 검색 닫기 시 검색 핀도 제거
- PC: 좌측 패널에 검색·필터·목록·상세 (지도앱 패턴)

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
