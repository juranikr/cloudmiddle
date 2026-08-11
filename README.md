# 중국 도시별 여행 지도

지난(济南)과 선양(沈阳)을 도시별로 분리해 관리하는 공유 여행 지도입니다.
FastAPI + React(Vite) + SQLite(기본) / PostgreSQL(선택) + Leaflet/OSM.

- 도시 선택 시 지도 중심·장소 목록·주소 검색 범위가 함께 전환됩니다.
- 기존 장소는 마이그레이션 시 지난에 자동 귀속됩니다.
- 에이전트의 자율 웹 조사·자동 장소 생성·자동 병합은 기본적으로 꺼져 있습니다.

**다른 PC/Cursor에서 이어서 개발:** 무조건 [`DEV_HISTORY.md`](DEV_HISTORY.md)를 먼저 읽으세요.  
(에이전트 규칙: `.cursor/rules/dev-history.mdc` — 매 작업 후 히스토리 갱신·GitHub push)

- 운영 HTTPS: https://d232kzujcg4ufp.cloudfront.net  
- AWS 배포(Terraform): [`infra/README.md`](infra/README.md)

## Docker 없이 로컬 실행 (권장)

현재처럼 Docker Desktop이 안 되는 Windows에서는 **SQLite**로 바로 돌리면 됩니다.  
(기본 `DATABASE_URL`이 SQLite입니다.)

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

## Docker / PostgreSQL (선택)

Docker Desktop이 되는 PC·서버에서는:

```bash
docker compose up --build -d
```

- API: http://localhost:8000  
- DB: Postgres (`jinan` / `jinan_secret` / `jinan_travel`)

호스트에서 API만 띄우고 Postgres에 붙일 때:

```bash
# Windows PowerShell
$env:DATABASE_URL="postgresql+psycopg2://jinan:jinan_secret@localhost:5432/jinan_travel"
uvicorn app.main:app --reload --port 8000
```

## Docker CLI만 설치하면 되나?

**안 됩니다.** `docker` CLI는 클라이언트일 뿐이고, 실제 컨테이너를 돌릴 **엔진(데몬)** 이 필요합니다.

| 방식 | 이 PC에서의 현실성 |
|------|-------------------|
| Docker Desktop | Windows 업데이트가 막히면 설치/실행이 자주 실패. 또한 비교적 최신 Windows + WSL2가 필요 |
| Docker CLI만 | 엔진이 없어 컨테이너 실행 불가 |
| WSL2 + Docker Engine | 이 PC에 WSL 자체가 없음 + OS 빌드가 오래됨(1903) |
| **SQLite (현재 기본)** | Docker 없이 바로 개발 가능 |

나중에 Windows를 업데이트하거나 서버(Linux)로 옮기면 `docker compose` / Postgres로 전환하면 됩니다.

## 사용법

1. 로그인
2. 유형 칩 / **전체·내 마커** 필터
3. **주소·장소 검색:** 운영 DB → ArcGIS → OSM Nominatim → Wikidata 후보를 도시 범위 안에서 통합. 출처·일치도·교차 확인 여부를 보고 선택
4. **내 위치:** 브라우저 GPS `watchPosition`으로 실시간 갱신. 지난시 대략 범위 안에 있을 때만 표시
5. **핀 찍기:** 지도 탭 → 위치 확인 후 하단 **입력/취소** → 내용 작성
6. **구역 선택:** 탭=수정 · 드래그=새 구역
7. 마커·구역 → 상세 (본인만 수정·삭제)

> Leaflet 자체에는 주소 검색이 없습니다. 백엔드 `/api/geocode`가 여러 공급자를 병렬 조회하고 근접 후보를 합칩니다.
> API 키 없는 ArcGIS 단독 결과는 라이선스상 참고 위치로만 표시되며, 등록하려면 지도에서 직접 지점을 지정합니다.
> `ARCGIS_API_KEY`를 설정하면 `forStorage=true` 검색으로 전환되어 ArcGIS 좌표도 바로 등록할 수 있습니다.

## 환경 변수

`backend/.env.example` 참고.
