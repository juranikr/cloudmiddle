import { useState, type FormEvent } from "react";
import * as api from "../api";
import type { ShareImportResult } from "../api";

interface Props {
  token: string;
  onImported: (result: ShareImportResult) => void;
}

export default function ShareImport({ token, onImported }: Props) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(e?: FormEvent) {
    e?.preventDefault();
    const payload = text.trim();
    if (!payload) return;
    setBusy(true);
    setError("");
    try {
      const result = await api.importShare(token, payload);
      onImported(result);
      setText("");
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "가져오기 실패");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="share-import">
      <button
        type="button"
        className={`share-import__toggle ${open ? "is-active" : ""}`}
        onClick={() => setOpen((v) => !v)}
      >
        따종·고덕 가져오기
      </button>
      {open ? (
        <form className="share-import__panel" onSubmit={handleSubmit}>
          <p className="share-import__hint">
            따종 공유 문구 전체, 또는 고덕 <code>surl.amap.com</code> 링크를 붙여넣으세요.
          </p>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={5}
            placeholder={"예)\n【가게이름】★★★★☆ 4.7\n¥93/人\n…\n주소 http://dpurl.cn/…\n\n또는\nhttps://surl.amap.com/…"}
          />
          {error ? <p className="share-import__error">{error}</p> : null}
          <div className="share-import__actions">
            <button type="button" className="btn btn--ghost" onClick={() => setOpen(false)}>
              닫기
            </button>
            <button type="submit" className="btn btn--primary" disabled={busy || !text.trim()}>
              {busy ? "해석 중…" : "가져오기"}
            </button>
          </div>
        </form>
      ) : null}
    </div>
  );
}
