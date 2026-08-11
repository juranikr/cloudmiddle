import { useCallback, useEffect, useState, type FormEvent } from "react";
import * as api from "../api";
import { useAuth } from "../auth";
import type {
  AdminAgentAction,
  AdminAgentProposal,
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

export default function AdminPage() {
  const { token, user, logout } = useAuth();
  const [status, setStatus] = useState<AdminStatus | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [actions, setActions] = useState<AdminAgentAction[]>([]);
  const [knowledge, setKnowledge] = useState<AdminKnowledge[]>([]);
  const [cities, setCities] = useState<City[]>([]);
  const [proposals, setProposals] = useState<AdminAgentProposal[]>([]);
  const [selectedCityId, setSelectedCityId] = useState(2);
  const [researchMode, setResearchMode] = useState(true);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");

  const reload = useCallback(async () => {
    if (!token) return;
    setError("");
    try {
      const [s, u, a, k, c, p] = await Promise.all([
        api.fetchAdminStatus(token),
        api.fetchAdminUsers(token),
        api.fetchAdminAgentActions(token),
        api.fetchAdminKnowledge(token),
        api.fetchCities(token),
        api.fetchAdminAgentProposals(token, selectedCityId),
      ]);
      setStatus(s);
      setUsers(u);
      setActions(a);
      setKnowledge(k);
      setCities(c);
      setProposals(p);
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
      // 백그라운드 실행 → 끝날 때까지 3초 간격 폴링 (최대 10분)
      const deadline = Date.now() + 10 * 60 * 1000;
      let status = await api.fetchAdminAgentStatus(token);
      while (status.running && Date.now() < deadline) {
        await new Promise((res) => setTimeout(res, 3000));
        status = await api.fetchAdminAgentStatus(token);
      }
      const r = status.result;
      if (r) {
        setInfo(
          `${r.ok ? "완료" : "실패"} · steps ${r.steps} · unread ${r.unread_before}→${r.unread_after}\n${r.message}`,
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

      <section className="admin__card">
        <h2>에이전트</h2>
        <div className="admin__agent-controls">
          <label>
            실행 도시
            <select value={selectedCityId} onChange={(e) => setSelectedCityId(Number(e.target.value))}>
              {cities.map((city) => (
                <option key={city.id} value={city.id}>
                  {city.name_ko} ({city.name_local}) · 장소 {city.place_count}
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
          <ul className="admin__stats">
            <li>
              Groq: {status.groq_configured ? "설정됨" : "미설정"} ({status.groq_model})
            </li>
            <li>활성 장소: {status.markers_active}</li>
            <li>
              미읽음 이력: {status.events_unread} / 전체 {status.events_total}
            </li>
            <li>열린 이의: {status.appeals_open}</li>
            <li>작업 대기(이력+이의): {status.unread_work_items}</li>
            <li>지식 주제: {status.knowledge_topics ?? 0}</li>
            <li>에이전트 추천 장소: {status.agent_suggested_places ?? 0}</li>
            <li>승인 대기 제안: {status.proposals_pending ?? 0}</li>
          </ul>
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
        <p className="panel__meta">
          매일 새벽 03:00(KST)에도 자동 실행됩니다. API 키는 AWS Secrets Manager
          (`tourmiddle-dev/app`의 GROQ_*)에서 관리합니다.
        </p>
      </section>

      <section className="admin__card admin__card--proposals">
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
      </section>

      <section className="admin__card">
        <h2>에이전트 변경 이력</h2>
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
      </section>

      
      <section className="admin__card">
        <h2>에이전트 지식베이스</h2>
        <p className="panel__meta">
          이의·롤백·웹조사에서 얻은 교훈을 주제별로 병합해 다음 실행에 사용합니다.
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
                </span>
                <pre>{k.content}</pre>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="admin__card">
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
      </section>
    </div>
  );
}
