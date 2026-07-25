import { useCallback, useEffect, useState, type FormEvent } from "react";
import * as api from "../api";
import { useAuth } from "../auth";
import type { AdminAgentAction, AdminKnowledge, AdminStatus, User } from "../types";

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
      const [s, u, a, k] = await Promise.all([
        api.fetchAdminStatus(token),
        api.fetchAdminUsers(token),
        api.fetchAdminAgentActions(token),
        api.fetchAdminKnowledge(token),
      ]);
      setStatus(s);
      setUsers(u);
      setActions(a);
      setKnowledge(k);
    } catch (e) {
      setError(e instanceof Error ? e.message : "관리자 정보를 불러오지 못했습니다");
    }
  }, [token]);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function runAgent() {
    if (!token) return;
    if (!confirm("에이전트를 지금 실행할까요? (Groq 호출·지도 변경 가능)")) return;
    setBusy(true);
    setError("");
    setInfo("");
    try {
      const r = await api.runAdminAgent(token);
      setInfo(
        `${r.ok ? "완료" : "실패"} · steps ${r.steps} · unread ${r.unread_before}→${r.unread_after}\n${r.message}`,
      );
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "에이전트 실행 실패");
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
          </ul>
        ) : (
          <p className="panel__meta">불러오는 중…</p>
        )}
        <div className="panel__actions">
          <button type="button" className="btn btn--primary" onClick={() => void runAgent()} disabled={busy}>
            {busy ? "실행 중…" : "에이전트 지금 실행"}
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
