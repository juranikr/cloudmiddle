import { useEffect, useState, type FormEvent } from "react";
import * as api from "../api";
import type { UserMessage } from "../types";
import PlaceIdBadge from "./PlaceIdBadge";

interface Props {
  token: string;
  open: boolean;
  onClose: () => void;
  onUnreadChange?: (n: number) => void;
  onOpenPlace?: (placeId: number) => void;
}

export default function MessageInbox({
  token,
  open,
  onClose,
  onUnreadChange,
  onOpenPlace,
}: Props) {
  const [items, setItems] = useState<UserMessage[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [appealFor, setAppealFor] = useState<UserMessage | null>(null);
  const [appealBody, setAppealBody] = useState("");

  async function reload() {
    setError("");
    try {
      const data = await api.fetchMessages(token);
      setItems(data);
      onUnreadChange?.(data.filter((m) => !m.read_at).length);
    } catch (e) {
      setError(e instanceof Error ? e.message : "메시지를 불러오지 못했습니다");
    }
  }

  useEffect(() => {
    if (open) void reload();
  }, [open, token]);

  async function markRead(id: number) {
    try {
      const updated = await api.markMessageRead(token, id);
      setItems((prev) => prev.map((m) => (m.id === id ? updated : m)));
      const next = await api.fetchUnreadMessageCount(token);
      onUnreadChange?.(next);
    } catch {
      /* ignore */
    }
  }

  async function markAll() {
    setBusy(true);
    try {
      await api.markAllMessagesRead(token);
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "처리 실패");
    } finally {
      setBusy(false);
    }
  }

  async function submitAppeal(e: FormEvent) {
    e.preventDefault();
    if (!appealFor?.place_id || !appealBody.trim()) return;
    setBusy(true);
    setError("");
    try {
      await api.createAppeal(token, {
        place_id: appealFor.place_id,
        body: appealBody.trim(),
        message_id: appealFor.id,
      });
      setAppealFor(null);
      setAppealBody("");
      await markRead(appealFor.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "이의신청 실패");
    } finally {
      setBusy(false);
    }
  }

  if (!open) return null;

  return (
    <div className="inbox" role="dialog" aria-label="메시지">
      <header className="inbox__header">
        <h2>메시지</h2>
        <div className="inbox__header-actions">
          <button type="button" className="btn btn--ghost" onClick={() => void markAll()} disabled={busy}>
            모두 읽음
          </button>
          <button type="button" className="panel__close" onClick={onClose} aria-label="닫기">
            ×
          </button>
        </div>
      </header>
      <div className="inbox__body">
        {error ? <p className="panel__error">{error}</p> : null}
        {appealFor ? (
          <form className="inbox__appeal" onSubmit={(e) => void submitAppeal(e)}>
            <p className="panel__meta">이의신청 · 장소 #{appealFor.place_id}</p>
            <textarea
              value={appealBody}
              onChange={(e) => setAppealBody(e.target.value)}
              rows={4}
              maxLength={4000}
              placeholder="왜 잘못되었는지, 어떻게 고쳐야 하는지 적어 주세요. 다음 새벽 정리 때 다시 봅니다."
              required
            />
            <div className="panel__actions">
              <button type="button" className="btn btn--ghost" onClick={() => setAppealFor(null)}>
                취소
              </button>
              <button type="submit" className="btn btn--primary" disabled={busy}>
                이의 남기기
              </button>
            </div>
          </form>
        ) : null}
        {items.length === 0 ? (
          <p className="panel__meta">메시지가 없습니다</p>
        ) : (
          <ul className="inbox__list">
            {items.map((m) => (
              <li key={m.id} className={`inbox__item ${m.read_at ? "" : "is-unread"}`}>
                <button
                  type="button"
                  className="inbox__item-main"
                  onClick={() => {
                    void markRead(m.id);
                    if (m.place_id) onOpenPlace?.(m.place_id);
                  }}
                >
                  <strong>{m.place_id ? <PlaceIdBadge id={m.place_id} /> : null}{m.title}</strong>
                  <span className="inbox__kind">{m.kind}</span>
                  <p>{m.body}</p>
                </button>
                {m.can_appeal ? (
                  <button
                    type="button"
                    className="btn btn--ghost inbox__appeal-btn"
                    onClick={() => {
                      setAppealFor(m);
                      setAppealBody("");
                    }}
                  >
                    이의신청
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
