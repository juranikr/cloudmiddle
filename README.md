# 중국 도시별 여행 지도

지난(济南)과 선양(沈阳)을 도시별로 분리해 관리하는 공유 여행 지도입니다.
FastAPI + React(Vite) + SQLite(기본) / PostgreSQL(선택) + Leaflet/OSM.

- 도시 선택 시 지도 중심·장소 목록·주소 검색 범위가 함께 전환됩니다.
- 기존 장소는 마이그레이션 시 지난에 자동 귀속됩니다.
- 에이전트는 도시별 큐·지식·검색 이력을 분리하고, 자동 생성·병합이 꺼져 있어도 근거 기반 승인 제안을 남깁니다.

**다른 PC/Cursor에서 이어서 개발:** 무조건 [`DEV_HISTORY.md`](DEV_HISTORY.md)를 먼저 읽으세요.  
(에이전트 규칙: `.cursor/rules/dev-history.mdc` — 매 작업 후 히스토리 갱신·GitHub push)

- 운영 HTTPS: https://d232kzujcg4ufp.cloudfront.net  
- AWS 배포(Terraform): [`infra/README.md`](infra/README.md)

## Docker로 배포 전 로컬 통합 실행 (권장)

로컬 통합 환경은 운영과 이름·포트·볼륨이 분리된 PostgreSQL과 실제 배포용
Dockerfile(UI 정적 빌드 + API)을 함께 실행합니다.

```powershell
# 키 없이 UI/API/DB만 확인
.\dev\local.ps1 up

# 배포 전 전체 smoke(unittest/build/compile/diff/Compose/API·로그인)
.\dev\predeploy.ps1

# 에이전트·검색 E2E도 확인할 때만 (파일은 gitignored)
Copy-Item .env.local.example .env.local
# .env.local에 로컬 테스트용 API 키 입력
.\dev\local.ps1 up -EnvFile .env.local
```

- 통합 UI/API: http://127.0.0.1:18000
- 로컬 PostgreSQL: `127.0.0.1:55432`, DB/사용자 `cloudmiddle_local`
- 영구 볼륨: `cloudmiddle_local_pgdata`
- 화면 상단의 `LOCAL INTEGRATION` 배지와 `/api/health`의
  `{"status":"ok","db_mode":"local"}`로 운영과 구분합니다.
- `GROQ_API_KEY` 등 선택 키를 전달해도 자율 연구·자동 생성·자동 병합은 기본적으로 꺼져 있습니다.

운영 명령:

```powershell
.\dev\local.ps1 status
.\dev\local.ps1 logs
.\dev\local.ps1 down       # 볼륨 보존

# 로컬 DB를 정말 초기화할 때만: cloudmiddle_local_pgdata 삭제
.\dev\local.ps1 reset -ConfirmReset RESET-cloudmiddle_local
```

Compose는 DB와 앱 포트를 `127.0.0.1`에만 바인딩합니다. `.env.local`에는
운영 `DATABASE_URL`이나 AWS 자격증명을 넣지 마세요.

### 운영 스냅샷을 로컬 DB로 복제

운영 문제를 실제 데이터 모양으로 재현해야 할 때만 아래 wrapper를 사용합니다. 이 도구는
AWS Secrets Manager에서 URL을 프로세스 메모리로만 읽고, source/target의 host·DB·user·port
allowlist를 확인한 다음 staging DB에 복원·검증하고 `cloudmiddle_local`로 교체합니다.
비밀번호는 명령 인자나 출력에 포함하지 않습니다.

```powershell
# 1) AWS 접근, 운영 source와 local target guard만 검증 (DB 연결/변경 없음)
.\dev\clone-production-db.ps1 -DryRun

# 2-a) 일반 로컬 회귀 테스트: 사용자 이메일/비밀번호를 로컬 값으로 바꾸고
#      채팅·메모·이의·일정·에이전트 trace 등 private content는 제거
.\dev\clone-production-db.ps1 -Confirmation RESET-cloudmiddle_local

# 2-b) 특정 운영 장애를 처음 정밀 진단할 때만 private content도 로컬에 보존
.\dev\clone-production-db.ps1 `
  -Confirmation RESET-cloudmiddle_local `
  -RetainPrivateContent
```

복제는 **로컬 DB를 교체하는 파괴적 작업**이며 정확한 확인 문구 없이는 실행되지 않습니다.
두 방식 모두 계정 이메일과 모든 비밀번호 해시는 운영 값과 분리되고,
로컬 로그인은 `test@test.com` / `test1234`로 통일됩니다. `-RetainPrivateContent` 결과는
민감한 운영 데이터이므로 이 노트북 밖으로 복사하거나 커밋하지 마세요.

정기 복제가 필요하면 private content 제거가 기본인 작업만 등록합니다.

```powershell
.\dev\register-db-clone-task.ps1 -Action Register -DailyAt 04:30
.\dev\register-db-clone-task.ps1 -Action Unregister
```

예약 작업은 현재 Windows 사용자 세션, Docker Desktop, AWS profile이 사용 가능한 경우에만
실행됩니다. 앱 컨테이너가 열린 연결을 갖고 있다면 복제 후 `.\dev\local.ps1 up`으로
재생성해 새 DB 연결을 확실히 사용하세요.

## 호스트 개발 실행 (Vite hot reload)

DB만 Docker로 실행하고 API/프론트는 호스트에서 실행할 수 있습니다.

```powershell
# 터미널 1
.\dev\local.ps1 db
$env:APP_DB_MODE="local"
$env:DATABASE_URL="postgresql+psycopg2://cloudmiddle_local:cloudmiddle_local_only@127.0.0.1:55432/cloudmiddle_local"
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# 터미널 2
cd frontend
npm install
npm run dev
```

브라우저: `http://localhost:5173`. `VITE_API_URL`을 비워 두면 Vite가 `/api`를
`127.0.0.1:8000`으로 프록시합니다.

## Docker 없이 SQLite 실행

Docker를 쓸 수 없는 환경에서는 **SQLite**로 바로 실행할 수 있습니다.

### 백엔드 (Python 3.11 + venv)

> 시스템 Python이 3.14면 일부 패키지가 설치되지 않습니다. **3.11**을 쓰세요.  
> (`winget install -e --id Python.Python.3.11`)

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

conda가 PATH에 있을 때 (다른 서버 재설치용):

```bash
cd backend
conda env create -f environment.yml
conda activate jinan-travel
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

DB 파일: `backend/jinan_travel.db` (자동 생성 + 시드 계정)

### 프론트엔드

```bash
cd frontend
npm install
npm run dev
```

브라우저: `http://localhost:5173` (기본 HTTP)

### 같은 Wi-Fi 모바일 접속

1. PC에서 API + `npm run dev`  
2. 폰: `http://<PC LAN IP>:5173`  

**내 위치 / 실제 GPS**
- 아이폰 Safari는 `http://192.168…` 에서 GPS를 **막을 수 없음**(설정 허용과 무관, 브라우저 보안)
- HTTP로 폰 접속 시 **내 위치** = 지도 중심 **가상 위치**(UI 확인용)
- 실제 GPS가 되는 경우만:
  - PC에서 `http://localhost:5173` (localhost는 예외로 허용)
  - 또는 HTTPS: PowerShell에서 `$env:VITE_DEV_HTTPS=1; npm run dev` 후 `https://IP:5173`

## 계정

운영 계정은 공개 README에 적지 않습니다. 접속 URL·인프라는 [`DEV_HISTORY.md`](DEV_HISTORY.md) §2를 보세요.  
로컬/점검용 테스트 계정만: `test@test.com` / `test1234`

## 운영 DB 읽기 전용 진단

운영 진단은 로컬/복제 DB 실행과 **별도 모드·별도 환경변수**를 씁니다. 운영 DB에
`SELECT` 권한만 가진 전용 사용자부터 준비해야 합니다. 일반 앱의 쓰기 가능한
`DATABASE_URL`을 재사용하지 마세요.

```powershell
# 현재 PowerShell 프로세스에만 주입 (명령/문서/파일에 실제 URL을 남기지 않음)
$env:PROD_READONLY_DATABASE_URL="postgresql+psycopg2://READONLY_USER:...@HOST:5432/DB"
.\dev\production-readonly.ps1
```

이 API는 `http://127.0.0.1:18001`에서만 열립니다. 두 번째 터미널에서 UI를 띄우려면:

```powershell
cd frontend
$env:VITE_API_URL="http://127.0.0.1:18001"
$env:VITE_RUNTIME_LABEL="PRODUCTION READ-ONLY"
npm run dev
```

`production_readonly` 모드는 다음을 중첩 적용합니다.

- PostgreSQL 연결에 `default_transaction_read_only=on` 및 transaction read-only 설정
- 시작 시 `create_all`, 스키마 보정, 시드, proposal task reconcile 전부 생략
- UI 로그인을 위한 정확한 `POST /api/auth/login`만 예외로 두고 나머지
  `POST/PATCH/PUT/DELETE`는 HTTP 503
- 진단 프로세스 전용 임시 JWT 서명 키를 매번 생성해 운영 토큰과 분리
- 모든 응답의 `X-Cloudmiddle-DB-Mode`와 `/api/health`에 현재 모드 표시

애플리케이션 경계는 보조 방어입니다. 운영 진단 계정 자체도 반드시 DB 권한이
읽기 전용이어야 합니다.

## 사용법

1. 로그인
2. 유형 칩 / **전체·내 마커** 필터
3. **주소·장소 검색:** 운영 DB → ArcGIS → OSM Nominatim → Wikidata 후보를 도시 범위 안에서 통합. 출처·일치도·교차 확인 여부를 보고 선택
4. **내 위치:** 브라우저 GPS `watchPosition`으로 실시간 갱신. 선택한 도시 범위 안에 있을 때만 표시
5. **핀 찍기:** 지도 탭 → 위치 확인 후 하단 **입력/취소** → 내용 작성
6. **구역 선택:** 탭=수정 · 드래그=새 구역
7. 마커·구역 → 상세 (본인만 수정·삭제)

장소 상세에는 좌표 출처·신뢰도·좌표계와 함께 위치 맥락, 역사 타임라인, 방문 정보, 현지 팁이 출처별로 표시됩니다. 관리자 `/admin`에서는 도시를 골라 조사 에이전트를 실행하고 신규 장소·병합 제안을 승인하거나 거절할 수 있습니다.

> Leaflet 자체에는 주소 검색이 없습니다. 백엔드 `/api/geocode`가 여러 공급자를 병렬 조회하고 근접 후보를 합칩니다.
> API 키 없는 ArcGIS 단독 결과는 라이선스상 참고 위치로만 표시되며, 등록하려면 지도에서 직접 지점을 지정합니다.
> `ARCGIS_API_KEY`를 설정하면 `forStorage=true` 검색으로 전환되어 ArcGIS 좌표도 바로 등록할 수 있습니다.

## 환경 변수

`backend/.env.example` 참고.
