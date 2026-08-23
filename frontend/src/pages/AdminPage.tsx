import { useCallback, useEffect, useState, type FormEvent } from "react";
import * as api from "../api";
import { useAuth } from "../auth";
import type {
  AdminAgentAction,
  AdminAgentNextCursor,
  AdminAgentOutcomeCategory,
  AdminAgentProposal,
  AdminAgentRunHistory,
  AdminAgentRunStep,
  AdminAgentTask,
  AdminAgentMission,
  AdminAgentWorkItem,
  AdminKnowledge,
  AdminStatus,
  City,
  User,
} from "../types";

const ACTION_LABEL: Record<string, string> = {
  merge: "병합",
  update: "수정",
  context_update: "컨텍스트",
  agent_create: "추천 추가",
  image_reorder: "이미지 순서",
};

const METRIC_LABEL: Record<string, string> = {
  unread_cleared: "작업 처리",
  proposals: "승인 제안",
  insights: "인사이트",
  images: "이미지",
  zoned_places: "구역 연결",
  chained_places: "체인 연결",
  completed_tasks: "과제 완료",
  role_diversity: "역할 다양성",
};

const OUTCOME_LABEL: Record<AdminAgentOutcomeCategory, string> = {
  traveler_visible_changed: "여행자 화면 변경",
  proposal_created: "승인 제안 생성",
  verified_or_waived_no_change: "검증 완료 · 변경 불필요",
  queue_acknowledged: "이력 학습 · 큐 정리",
  deferred_or_blocked: "조건 대기 · 차단",
  no_yield: "성과 없음",
  failed: "실행 실패",
};

function nextCursorLabel(cursor: AdminAgentNextCursor | undefined, workItemId: number | null | undefined): string {
  const id = workItemId ?? cursor?.work_item_id;
  const parts: string[] = [];
  if (id) parts.push(`작업 #${id}`);
  else if (cursor?.mission_id) parts.push(`미션 #${cursor.mission_id}`);
  if (cursor?.target) parts.push(cursor.target);
  if (cursor?.next_tool) parts.push(`다음 행동 ${cursor.next_tool}`);
  if (cursor?.wait_reason) parts.push(`재개 조건 ${cursor.wait_reason}`);
  if (parts.length) return parts.join(" · ");
  return "저장된 후속 커서 없음";
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function runDelta(run: AdminAgentRunHistory): Array<[string, number]> {
  return Object.entries(asRecord(run.metrics.delta))
    .map(([key, value]) => [key, Number(value)] as [string, number])
    .filter(([, value]) => Number.isFinite(value) && value !== 0);
}

function runMaterialChanges(run: AdminAgentRunHistory): Record<string, unknown>[] {
  const value = run.metrics.material_changes;
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => !!item && typeof item === "object") : [];
}

function runDiscoveryFunnel(run: AdminAgentRunHistory): Array<[string, number]> {
  const funnel = asRecord(run.metrics.discovery_funnel);
  const labels: Array<[string, string]> = [
    ["search_calls", "검색"],
    ["place_discovery_calls", "장소 발견 호출"],
    ["raw_hits", "원시 결과"],
    ["exposed_hits", "필터 통과"],
    ["validated_pages", "본문 검증"],
    ["geocode_candidates", "좌표 후보"],
    ["proposal_attempts", "제안 시도"],
    ["proposals_created", "제안 생성"],
  ];
  return labels
    .map(([key, label]) => [label, Number(funnel[key] ?? 0)] as [string, number])
    .filter(([, value]) => Number.isFinite(value));
}

function stepDescription(step: AdminAgentRunStep): string {
  const args = asRecord(step.detail.args);
  const result = asRecord(step.detail.result);
  const target = args.title ?? args.query ?? args.url ?? (args.place_id ? `장소 #${args.place_id}` : "");
  const outcome = result.error ?? result.detail ?? result.proposal_id ?? result.changed ?? "";
  return [target, outcome].filter((value) => value !== "" && value != null).map(String).join(" · ").slice(0, 240);
}

export default function AdminPage() {
  const { token, user, logout } = useAuth();
  const [status, setStatus] = useState<AdminStatus | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [actions, setActions] = useState<AdminAgentAction[]>([]);
  const [knowledge, setKnowledge] = useState<AdminKnowledge[]>([]);
  const [cities, setCities] = useState<City[]>([]);
  const [proposals, setProposals] = useState<AdminAgentProposal[]>([]);
  const [runs, setRuns] = useState<AdminAgentRunHistory[]>([]);
  const [runSteps, setRunSteps] = useState<Record<number, AdminAgentRunStep[]>>({});
  const [loadingRunId, setLoadingRunId] = useState<number | null>(null);
  const [tasks, setTasks] = useState<AdminAgentTask[]>([]);
  const [missions, setMissions] = useState<AdminAgentMission[]>([]);
  const [missionItems, setMissionItems] = useState<Record<number, AdminAgentWorkItem[]>>({});
  const [selectedCityId, setSelectedCityId] = useState(2);
  const [researchMode, setResearchMode] = useState(true);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [activeTab, setActiveTab] = useState<"agent" | "proposals" | "history" | "knowledge" | "users">("agent");

  const reload = useCallback(async () => {
    if (!token) return;
    setError("");
    try {
      const [s, u, a, k, c, p, r, t, m] = await Promise.all([
        api.fetchAdminStatus(token),
        api.fetchAdminUsers(token),
        api.fetchAdminAgentActions(token, selectedCityId),
        api.fetchAdminKnowledge(token),
        api.fetchCities(token),
        api.fetchAdminAgentProposals(token, selectedCityId),
        api.fetchAdminAgentRuns(token, selectedCityId),
        api.fetchAdminAgentTasks(token, selectedCityId),
        api.fetchAdminAgentMissions(token, selectedCityId),
      ]);
      setStatus(s);
      setUsers(u);
      setActions(a);
      setKnowledge(k);
      setCities(c);
      setProposals(p);
      setRuns(r);
      setTasks(t);
      setMissions(m);
      const itemEntries = await Promise.all(m.map(async (mission) => [
        mission.id,
        await api.fetchAdminAgentWorkItems(token, mission.id),
      ] as const));
      setMissionItems(Object.fromEntries(itemEntries));
    } catch (e) {
      setError(e instanceof Error ? e.message : "관리자 정보를 불러오지 못했습니다");
    }
  }, [token, selectedCityId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function runAgent() {
    if (!token) return;
    if (!confirm("에이전트를 지금 실행할까요? (Groq 호출·지도 변경 가능)")) return;
    setBusy(true);
    setError("");
    setInfo("실행 중… (수 분 걸릴 수 있습니다)");
    try {
      await api.startAdminAgent(token, selectedCityId, researchMode);
      // Step Functions/Fargate 실행 → 3초 간격 폴링. 근거 수집과 모델
      // 복구 재시도를 포함한 정상 장기 실행도 브라우저가 충분히 기다린다.
      const deadline = Date.now() + 30 * 60 * 1000;
      let status = await api.fetchAdminAgentStatus(token, selectedCityId);
      while (status.running && Date.now() < deadline) {
        await new Promise((res) => setTimeout(res, 3000));
        status = await api.fetchAdminAgentStatus(token, selectedCityId);
      }
      const r = status.result;
      if (r) {
        const resultLabel = r.status === "completed" ? "완료" : r.status === "partial" ? "부분 완료" : "실패";
        const outcome = status.outcome_category ?? (r.status === "failed" ? "failed" : "no_yield");
        setInfo(
          `${OUTCOME_LABEL[outcome]} · 실질 변경 ${status.material_change_count ?? 0}건\n` +
          `다음 커서: ${nextCursorLabel(status.next_cursor, status.next_work_item_id)}\n` +
          `시스템 ${resultLabel} · 성과 ${r.score ?? 0}점 · 행동 라운드 ${r.steps} · 미처리 ${r.unread_before}→${r.unread_after}\n${r.message}`,
        );
      } else if (status.running) {
        setInfo("아직 실행 중입니다. 잠시 후 새로고침으로 결과를 확인하세요.");
      } else {
        setInfo("실행 결과를 확인하지 못했습니다. 새로고침해 주세요.");
      }
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "에이전트 실행 실패");
    } finally {
      setBusy(false);
    }
  }

  async function loadRunSteps(runId: number) {
    if (!token || runSteps[runId] || loadingRunId === runId) return;
    setLoadingRunId(runId);
    try {
      const rows = await api.fetchAdminAgentRunSteps(token, runId);
      setRunSteps((current) => ({ ...current, [runId]: rows }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "실행 단계 이력을 불러오지 못했습니다.");
    } finally {
      setLoadingRunId(null);
    }
  }

  async function decideProposal(proposal: AdminAgentProposal, decision: "approve" | "reject") {
    if (!token) return;
    const verb = decision === "approve" ? "승인해 지도에 반영" : "거절";
    if (!confirm(`이 제안을 ${verb}할까요?\n${proposal.title}`)) return;
    const note = prompt("판단 메모(선택)", "") ?? "";
    setBusy(true);
    setError("");
    try {
      await api.decideAdminAgentProposal(token, proposal.id, decision, note);
      setInfo(`제안 #${proposal.id} ${decision === "approve" ? "승인·반영" : "거절"} 완료`);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "제안 처리 실패");
    } finally {
      setBusy(false);
    }
  }

  async function onCreateUser(e: FormEvent) {
    e.preventDefault();
    if (!token) return;
    setBusy(true);
    setError("");
    try {
      await api.createAdminUser(token, {
        email: email.trim(),
        display_name: displayName.trim(),
        password,
      });
      setEmail("");
      setDisplayName("");
      setPassword("");
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "사용자 추가 실패");
    } finally {
      setBusy(false);
    }
  }

  async function resetPassword(u: User) {
    if (!token) return;
    const pw = prompt(`${u.display_name} 새 비밀번호 (4자 이상)`);
    if (!pw || pw.length < 4) return;
    setBusy(true);
    try {
      await api.updateAdminUser(token, u.id, { password: pw });
      setInfo(`${u.email} 비밀번호를 변경했습니다`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "변경 실패");
    } finally {
      setBusy(false);
    }
  }

  async function removeUser(u: User) {
    if (!token) return;
    if (!confirm(`${u.email} 계정을 삭제할까요?`)) return;
    setBusy(true);
    try {
      await api.deleteAdminUser(token, u.id);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "삭제 실패");
    } finally {
      setBusy(false);
    }
  }

  async function rollbackAction(a: AdminAgentAction) {
    if (!token) return;
    const label = ACTION_LABEL[a.action] || a.action;
    const note =
      prompt(
        `#${a.id} (${label}) 롤백합니다.\n다음 에이전트 실행에 전달할 메모(선택):`,
        "",
      ) ?? null;
    if (note === null) return;
    if (!confirm(`에이전트 조치 #${a.id}를 롤백할까요?\n${a.summary}`)) return;
    setBusy(true);
    setError("");
    setInfo("");
    try {
      const r = await api.rollbackAdminAgentAction(token, a.id, note.trim());
      setInfo(`롤백 완료 · 이벤트 #${r.rollback_event_id}\n${r.message}`);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "롤백 실패");
    } finally {
      setBusy(false);
    }
  }

  if (!user?.is_admin) {
    return (
      <div className="admin">
        <p className="panel__error">관리자만 접근할 수 있습니다.</p>
        <a href="/">지도로 돌아가기</a>
      </div>
    );
  }

  return (
    <div className="admin">
      <header className="admin__header">
        <div>
          <h1>관리</h1>
          <p className="panel__meta">{user.display_name} · {user.email}</p>
        </div>
        <div className="admin__header-actions">
          <a className="btn btn--ghost" href="/">
            지도
          </a>
          <button type="button" className="btn btn--ghost" onClick={logout}>
            로그아웃
          </button>
        </div>
      </header>

      {error ? <p className="panel__error">{error}</p> : null}
      {info ? <pre className="admin__info">{info}</pre> : null}

      <nav className="admin__tabs" aria-label="관리 화면">
        {([
          ["agent", "운영"],
          ["proposals", `승인 제안 ${proposals.length ? `(${proposals.length})` : ""}`],
          ["history", "시스템 이력"],
          ["knowledge", "지식"],
          ["users", "사용자"],
        ] as const).map(([key, label]) => (
          <button key={key} type="button" className={activeTab === key ? "is-active" : ""} onClick={() => setActiveTab(key)}>{label}</button>
        ))}
      </nav>

      {activeTab === "agent" ? <section className="admin__card">
        <h2>에이전트</h2>
        <div className="admin__agent-controls">
          <label>
            실행 도시
            <select value={selectedCityId} onChange={(e) => setSelectedCityId(Number(e.target.value))}>
              {cities.map((city) => (
                <option key={city.id} value={city.id}>
                  {city.name_ko} ({city.name_local}) · 장소 {city.place_count} · 구역 {city.zone_count ?? 0}
                </option>
              ))}
            </select>
          </label>
          <label className="admin__check">
            <input
              type="checkbox"
              checked={researchMode}
              onChange={(e) => setResearchMode(e.target.checked)}
            />
            웹 조사·신규 장소 제안까지 수행
          </label>
        </div>
        {status ? (
          <>
            <ul className="admin__stats">
              <li>
                Groq: {status.groq_configured ? "설정됨" : "미설정"} ({status.groq_model})
              </li>
              <li>
                Brave Place: {status.brave_place_configured ? "발견용 연결됨" : "미설정"}
                {status.brave_storage_rights ? " · 저장 권한 있음" : " · 발견 전용 · 응답 데이터 비보존"}
              </li>
              <li>활성 장소: {status.markers_active}</li>
              <li>관광 구역: {status.zones_active ?? 0}</li>
              <li>
                미읽음 이력: {status.events_unread} / 전체 {status.events_total}
              </li>
              <li>열린 이의: {status.appeals_open}</li>
              <li>작업 대기(이력+이의): {status.unread_work_items}</li>
              <li>지식 주제: {status.knowledge_topics ?? 0}</li>
              <li>에이전트 추천 장소: {status.agent_suggested_places ?? 0}</li>
              <li>승인 대기 제안: {status.proposals_pending ?? 0}</li>
              <li>조건 변경까지 보류한 품질 결손: {status.quality_gaps_suppressed ?? 0}</li>
            </ul>
            {(status.events_unattributed ?? 0) > 0 ? (
              <aside className="admin__quarantine-warning" role="alert">
                <strong>도시 미귀속 격리 이력 {status.events_unattributed}건</strong>
                <span>
                  아직 city_id가 없어 도시별 에이전트가 안전하게 처리할 수 없는 미읽음 과거 이력입니다.
                  자동 처리된 것으로 숨기지 않았으므로 관리자 확인·귀속이 필요합니다.
                </span>
              </aside>
            ) : null}
          </>
        ) : (
          <p className="panel__meta">불러오는 중…</p>
        )}
        <div className="panel__actions">
          <button type="button" className="btn btn--primary" onClick={() => void runAgent()} disabled={busy}>
            {busy ? "실행 중…" : `${cities.find((c) => c.id === selectedCityId)?.name_ko ?? "도시"} 에이전트 실행`}
          </button>
          <button type="button" className="btn btn--ghost" onClick={() => void reload()} disabled={busy}>
            새로고침
          </button>
        </div>
        <div className="admin__continuity">
          <h3>지속 작업 기억</h3>
          {missions.length === 0 ? (
            <p className="panel__meta">아직 활성 Mission이 없습니다. 다음 연구 실행부터 작업 단위 기억이 생성됩니다.</p>
          ) : missions.map((mission) => {
            const items = missionItems[mission.id] ?? [];
            const active = items.find((item) => item.status === "active") ?? items.find((item) => item.status === "ready");
            return <article key={mission.id} className="admin__mission">
              <div>
                <strong>Mission #{mission.id} · {mission.title}</strong>
                <span>{mission.status} · 우선순위 {mission.priority}</span>
              </div>
              <p>{mission.success_metric || mission.objective}</p>
              {active ? <div className="admin__checkpoint">
                <b>현재: {active.target_key} · {active.title}</b>
                <span>{active.stage} / {active.status}</span>
                {active.state_summary ? <p>{active.state_summary}</p> : null}
                <code>다음 행동: {JSON.stringify(active.next_action)}</code>
                {active.failed_approaches.length ? <details>
                  <summary>실패한 경로 {active.failed_approaches.length}건</summary>
                  <ul>{active.failed_approaches.map((failure) => <li key={failure}>{failure}</li>)}</ul>
                </details> : null}
              </div> : null}
              <small>
                세부 작업 {items.filter((item) => item.status === "done").length}/{items.length} 완료 ·
                차단 {items.filter((item) => item.status === "blocked").length}
              </small>
            </article>;
          })}
        </div>
        <p className="panel__meta">
          매일 03:00·11:00·19:00(KST)에 성과 기반 자동 실행됩니다. API 키는 AWS Secrets Manager
          (`tourmiddle-dev/app`의 GROQ_*·BRAVE_*)에서 관리합니다.
        </p>
      </section> : null}

      {activeTab === "proposals" ? <section className="admin__card admin__card--proposals">
        <h2>근거 기반 승인 대기 ({proposals.length})</h2>
        <p className="panel__meta">
          에이전트가 찾은 신규 장소와 병합 후보입니다. 출처와 신뢰도를 확인한 뒤 반영하세요.
        </p>
        {proposals.length === 0 ? (
          <p className="panel__meta">선택한 도시에 승인 대기 제안이 없습니다.</p>
        ) : (
          <ul className="admin__proposals">
            {proposals.map((proposal) => (
              <li key={proposal.id}>
                <div className="admin__proposal-head">
                  <strong>{proposal.title}</strong>
                  <span className={`confidence confidence--${proposal.confidence >= 0.8 ? "high" : "mid"}`}>
                    신뢰도 {Math.round(proposal.confidence * 100)}%
                  </span>
                </div>
                <span className="panel__meta">
                  #{proposal.id} · {proposal.action === "create_place" ? "신규 장소" : "장소 병합"} · {new Date(proposal.created_at).toLocaleString("ko-KR")}
                </span>
                <p>{proposal.evidence || "근거 설명 없음"}</p>
                {proposal.action === "create_place" ? (
                  <dl className="admin__proposal-data">
                    <div><dt>좌표</dt><dd>{String(proposal.payload.lat ?? "-")}, {String(proposal.payload.lng ?? "-")}</dd></div>
                    <div><dt>분류</dt><dd>{String(proposal.payload.category ?? "other")}</dd></div>
                  </dl>
                ) : null}
                {proposal.source_urls.length ? (
                  <div className="admin__proposal-sources">
                    {proposal.source_urls.map((url) => (
                      <a key={url} href={url} target="_blank" rel="noreferrer">출처 보기</a>
                    ))}
                  </div>
                ) : null}
                <div className="panel__actions">
                  <button type="button" className="btn btn--primary" disabled={busy} onClick={() => void decideProposal(proposal, "approve")}>승인·반영</button>
                  <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => void decideProposal(proposal, "reject")}>거절</button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section> : null}

      {activeTab === "history" ? <section className="admin__card">
        <h2>에이전트 시스템 이력</h2>
        <p className="panel__meta">실행 로그와 후속 과제는 장소 설명·지식베이스와 분리되어 보관됩니다.</p>
        {runs.length ? (
          <ul className="admin__runs">
            {runs.map((run) => {
              const metrics = asRecord(run.metrics);
              const delta = runDelta(run);
              const material = runMaterialChanges(run);
              const discoveryFunnel = runDiscoveryFunnel(run);
              const steps = runSteps[run.id];
              const duration = Number(metrics.duration_seconds ?? 0);
              const materialCount = Number(run.material_change_count ?? material.length);
              const outcomeCategory = run.outcome_category ?? (run.status === "failed" ? "failed" : "no_yield");
              return (
                <li key={run.id}>
                  <div className="admin__run-heading">
                    <strong>실행 #{run.id} · {run.score.toFixed(1)}점</strong>
                    <span className={`run-outcome run-outcome--${outcomeCategory}`}>{OUTCOME_LABEL[outcomeCategory]}</span>
                  </div>
                  <p>{run.objective}</p>
                  <small>
                    {new Date(run.started_at).toLocaleString("ko-KR")} · 시스템 {run.status} · 도구 행동 {run.step_count}회
                    {duration > 0 ? ` · ${Math.round(duration / 60)}분` : ""}
                    {Number(metrics.repeated_calls_blocked ?? 0) > 0 ? ` · 반복 차단 ${metrics.repeated_calls_blocked}회` : ""}
                  </small>
                  <div className="admin__run-outcome-summary">
                    <strong>실질 변경 {Number.isFinite(materialCount) ? materialCount : material.length}건</strong>
                    <span>다음 커서: {nextCursorLabel(run.next_cursor, run.next_work_item_id)}</span>
                  </div>
                  <div className="admin__run-delta">
                    {delta.length ? delta.map(([key, value]) => (
                      <span key={key}>{METRIC_LABEL[key] ?? key} {value > 0 ? "+" : ""}{value}</span>
                    )) : <span className="is-empty">측정된 DB 변화 없음</span>}
                  </div>
                  {discoveryFunnel.some(([, value]) => value > 0) ? (
                    <div className="admin__run-delta" aria-label="신규 장소 발굴 퍼널">
                      {discoveryFunnel.map(([label, value]) => <span key={label}>{label} {value}</span>)}
                    </div>
                  ) : null}
                  {material.length ? (
                    <ul className="admin__run-material">
                      {material.slice(0, 8).map((item, index) => (
                        <li key={`${String(item.sequence ?? index)}-${String(item.tool ?? "change")}`}>
                          #{String(item.sequence ?? index + 1)} {String(item.tool ?? "변경")}
                          {item.proposal_id ? ` · 제안 #${String(item.proposal_id)}` : ""}
                          {item.place_id ? ` · 장소 #${String(item.place_id)}` : ""}
                          {item.task_id ? ` · 과제 #${String(item.task_id)}` : ""}
                          {item.title ? ` · ${String(item.title)}` : ""}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  <details><summary>결과 요약</summary><pre>{run.summary}</pre></details>
                  <details onToggle={(event) => { if (event.currentTarget.open) void loadRunSteps(run.id); }}>
                    <summary>전체 과정 {run.step_count}단계</summary>
                    {loadingRunId === run.id ? <p className="panel__meta">단계 이력을 불러오는 중…</p> : null}
                    {steps ? (
                      <ol className="admin__run-steps">
                        {steps.map((step) => (
                          <li key={step.sequence} className={`run-step run-step--${step.outcome}`}>
                            <strong>#{step.sequence} {step.tool}</strong>
                            <span>{step.outcome}{step.score_delta ? ` · +${step.score_delta}점` : ""}</span>
                            {stepDescription(step) ? <p>{stepDescription(step)}</p> : null}
                          </li>
                        ))}
                      </ol>
                    ) : null}
                  </details>
                </li>
              );
            })}
          </ul>
        ) : <p className="panel__meta">새 성과 기반 실행 이력이 아직 없습니다.</p>}
        <details className="admin__tasks" open>
          <summary><strong>다음 실행 백로그 ({tasks.length})</strong></summary>
          {tasks.length ? <ul>{tasks.map((task) => <li key={task.id}><strong>{task.title}</strong><span>우선순위 {task.priority} · {task.success_metric || task.kind}</span><p>{task.detail}</p></li>)}</ul> : <p className="panel__meta">대기 중인 후속 과제가 없습니다.</p>}
        </details>
        <h3>장소 변경·롤백 이력</h3>
        <p className="panel__meta">
          롤백하면 지도가 이전 상태로 돌아가고, 다음 에이전트 실행 시 같은 방향의 수정을
          피하도록 이력이 전달됩니다.
        </p>
        {actions.length === 0 ? (
          <p className="panel__meta">에이전트 변경 이력이 없습니다.</p>
        ) : (
          <ul className="admin__actions">
            {actions.map((a) => (
              <li key={a.id}>
                <div>
                  <strong>
                    #{a.id} · {ACTION_LABEL[a.action] || a.action}
                    {a.rolled_back ? " · 롤백됨" : ""}
                  </strong>
                  <span>
                    {a.place_title
                      ? `${a.place_title}${a.place_id ? ` (#${a.place_id})` : ""}`
                      : a.place_id
                        ? `장소 #${a.place_id}`
                        : "장소 없음"}
                    {" · "}
                    {new Date(a.created_at).toLocaleString("ko-KR")}
                  </span>
                  <span className="admin__action-summary">{a.summary}</span>
                </div>
                <div className="admin__user-actions">
                  {a.can_rollback ? (
                    <button
                      type="button"
                      className="btn btn--danger"
                      onClick={() => void rollbackAction(a)}
                      disabled={busy}
                    >
                      롤백
                    </button>
                  ) : (
                    <span className="panel__meta">{a.rolled_back ? "완료" : "불가"}</span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section> : null}

      
      {activeTab === "knowledge" ? <section className="admin__card">
        <h2>에이전트 지식베이스</h2>
        <p className="panel__meta">
          전체 문서를 무조건 넣지 않고 현재 도시·장소·과제·실패 상황과 관련도가 높은 항목만 검색해 다음 실행에 사용합니다.
        </p>
        {knowledge.length === 0 ? (
          <p className="panel__meta">아직 저장된 지식이 없습니다. 에이전트를 실행하면 쌓입니다.</p>
        ) : (
          <ul className="admin__knowledge">
            {knowledge.map((k) => (
              <li key={k.id}>
                <strong>
                  {k.title} <span className="panel__meta">({k.topic})</span>
                </strong>
                <span className="panel__meta">
                  {new Date(k.updated_at).toLocaleString("ko-KR")}
                  {k.place_id ? ` · 장소 #${k.place_id}` : ""}
                  {` · 사용 ${k.retrieval_count ?? 0}회`}
                </span>
                <p>{k.summary || k.content}</p>
                {k.keywords?.length ? <div className="admin__knowledge-tags">
                  {k.keywords.slice(0, 10).map((keyword) => <span key={keyword}>{keyword}</span>)}
                </div> : null}
                {k.principles?.length ? <ul>{k.principles.map((item) => <li key={item}>{item}</li>)}</ul> : null}
                {k.next_actions?.length ? <details><summary>다음 과제 {k.next_actions.length}개</summary><ul>{k.next_actions.map((item) => <li key={item}>{item}</li>)}</ul></details> : null}
                {k.applicability && Object.keys(k.applicability).length ? <details>
                  <summary>적용 조건</summary><pre>{JSON.stringify(k.applicability, null, 2)}</pre>
                </details> : null}
              </li>
            ))}
          </ul>
        )}
      </section> : null}

      {activeTab === "users" ? <section className="admin__card">
        <h2>사용자 ({status?.users_total ?? users.length})</h2>
        <ul className="admin__users">
          {users.map((u) => (
            <li key={u.id}>
              <div>
                <strong>{u.display_name}</strong>
                <span>{u.email}</span>
              </div>
              <div className="admin__user-actions">
                <button type="button" className="btn btn--ghost" onClick={() => void resetPassword(u)} disabled={busy}>
                  비밀번호
                </button>
                <button type="button" className="btn btn--danger" onClick={() => void removeUser(u)} disabled={busy}>
                  삭제
                </button>
              </div>
            </li>
          ))}
        </ul>
        <form className="admin__form" onSubmit={(e) => void onCreateUser(e)}>
          <h3>사용자 추가</h3>
          <label>
            이메일
            <input value={email} onChange={(e) => setEmail(e.target.value)} type="email" required />
          </label>
          <label>
            표시 이름
            <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} required maxLength={100} />
          </label>
          <label>
            비밀번호
            <input
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              type="password"
              required
              minLength={4}
            />
          </label>
          <button type="submit" className="btn btn--primary" disabled={busy}>
            추가
          </button>
        </form>
      </section> : null}
    </div>
  );
}
