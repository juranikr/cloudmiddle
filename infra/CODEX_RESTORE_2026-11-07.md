# 2026-11-07 서비스 복구 및 Codex 전환 기록

## 운영 상태

- 앱: https://d232kzujcg4ufp.cloudfront.net
- 배포 코드: `fd2092bb0e1a55e6353d903fc9582a09d4e95474`
- 이미지: `sha256:ea48a6113670449a99dc02f0c7b68f4241e1871f7a1bf5917721792772ed7d8d`
- API task definition: `tourmiddle-dev-api:7`, CPU 512 / memory 1024 MiB
- Batch task definition: `tourmiddle-dev-agent:5`, CPU 512 / memory 1024 MiB
- ECS API desired/running/pending: `1/1/0`, rollout `COMPLETED`
- `AGENT_BACKEND=codex`, `CODEX_MODEL=gpt-6.1-sol`
- 공식 Python SDK/CLI: `openai-codex==0.167.0`
- Secrets Manager `tourmiddle-dev/app`의 `OPENAI_API_KEY`를 ECS `CODEX_API_KEY`로 주입
- GitHub 자동 배포 재활성화, 테스트 실패 시 이미지 빌드/배포 중단
- 정기 유료 연구 rule은 `DISABLED` 유지; 관리 화면 수동 배치는 사용 가능

공유 RDS·ALB·VPC·클러스터를 재생성하거나 다른 앱을 재개하지 않았다.
기존 DB에는 대화 작업용 `travel_chat_jobs` 테이블만 추가했다.

## 구조 변경

기존 FastAPI/지도 API는 유지하고 대화 및 배치의 추론·도구 호출 루프를
공식 Codex harness로 전환했다. 여행 검색·좌표 검증·승인 제안·사진·구역 도구는
도시 범위가 고정된 전용 MCP 서버가 실행한다. 직접 장소 생성·병합·되돌리기와
관리 과제의 임의 완료 처리는 노출하지 않는다.

대화 요청은 HTTP 202 작업으로 접수하고 저장된 결과를 조회한다. 모델 제한시간은
대화 900초, 배치 7200초이며 HTTP 연결 제한에 맞춰 줄이지 않았다. 긴 실행은
별도 실행 풀에서 처리한다. 브라우저의 조회 재시도는 모델 작업을 재실행하지 않는다.

SQL의 사용자/도시별 기록, 후보, 실제 도구 결과, 체크포인트를 다음 실행에
다시 전달한다. Codex의 임시 세션 파일에 연속성을 의존하지 않는다. 저장 성공 후
응답이 끊긴 경우에도 제안과 대화 원장을 대조해 실제 성공 결과를 복구한다.

## 검증

- GitHub Actions run `42347612502`: 성공
- 백엔드 420개 테스트 통과, 프론트 TypeScript 및 production build 성공
- Linux 컨테이너 안의 `codex-cli 0.167.0` 실행 확인
- 로컬 격리 DB에서 실제 SDK + MCP 조회 및 후속 대화 통과
- 운영 health, 테스트 계정 로그인 및 지도 조회 성공
- 운영 대화 job `bedc29a3-df68-4598-b2e5-01345690333f`: 실제 `get_place` 호출,
  장소 #113 조회, 모델 `gpt-6.1-sol`
- 후속 job `46b0cd47-ff2d-4f1a-a503-69ed64f890b4`: 같은 장소 #113을 이어서 답변
- 운영 장소 전체 106행은 복구 전후 동일; 선양 지도 응답 44행
- 관리자 상태 API가 Codex backend/model/configured를 올바르게 보고
- 적용 후 Terraform plan: `No changes`

운영 배치 `AgentRun #382`도 새 Fargate task에서 실행했다. 배포 검증을 위해
도구 실행 상한 20회, 제한시간 600초로 한 번만 실행했으며 운영 기본값은 바꾸지
않았다. 실제 실행은 13회(지도/연구 이력 조회, 웹검색, 본문 읽기, 좌표 조회,
체크포인트) 후 정상 종료(exit 0), 상태 `partial`, 실제 변경 0건이었다.

조사 대상은 八经街 동네 산책 후보였다. 독립 출처와 정확한 골목 범위가 충분하지
않아 승인 제안을 보류했다. 도구 오류는 없었고, 확보한 근거·보류 이유·다음 행동·
재시도 조건이 DB에 저장됐다. 이번 검증을 신규 장소 수집 성과로 계산하지 않는다.

## 재개/중지 및 참고

현재 로컬 `terraform.tfvars`는 `deployment_mode=active`,
`agent_backend=codex`, `agent_schedule_enabled=false`다.
다시 쉬려면 `deployment_mode=paused`로 변경하고 검토한 plan을 적용한다.
공유 기반에는 기존 삭제 방지가 유지된다.

기존 백업 위치와 재해 복구는 [COLD_STORAGE.md](./COLD_STORAGE.md),
Codex 구성은 [backend/CODEX_HARNESS.md](../backend/CODEX_HARNESS.md)를 따른다.
