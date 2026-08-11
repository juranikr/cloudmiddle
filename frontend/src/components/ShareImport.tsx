import { useState, type FormEvent } from "react";
import * as api from "../api";
import type { ShareImportResult } from "../api";

type Source = "amap" | "dianping";

interface Props {
  token: string;
  cityId: number;
  source: Source;
  /** 메인(고덕) / 등록 패널(따종) */
  placement: "main" | "panel";
  onImported: (result: ShareImportResult) => void;
}

const COPY: Record<Source, { button: string; title: string; hint: string; placeholder: string }> = {
  amap: {
    button: "고덕 공유하기로 초안만들기",
    title: "고덕지도 공유 → 등록 초안",
    hint: "고덕에서 공유한 전문(명칭·가격·주소·링크)을 붙여넣으세요. 좌표·명칭이 자동으로 채워집니다.",
    placeholder:
      "예)\n桥下把子肉\n¥22/사람·중국 음식\n工业南路68号华润置地广场\nhttps://surl.amap.com/…",
  },
  dianping: {
    button: "따종 공유하기로 초안만들기",
    title: "따종 공유 → 이 위치에 초안",
    hint: "이미 찍은 위치는 유지됩니다. 따종 공유 문구를 붙여넣으면 이름·설명·링크만 채웁니다.",
    placeholder:
      "예)\n【燕喜堂·中华老字号(CBD店)】★★★★☆ 4.7\n¥93/人\n解放东路 鲁菜\n山左路与秦公街交叉口东北角 http://dpurl.cn/…",
  },
};

export default function ShareImport({ token, cityId, source, placement, onImported }: Props) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const copy = COPY[source];

  async function handleSubmit(e?: FormEvent) {
    e?.preventDefault();
    const payload = text.trim();
    if (!payload) return;
    setBusy(true);
    setError("");
    try {
      const result = await api.importShare(token, payload, source, cityId);
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
    <div className={`share-import share-import--${placement}`}>
      <button
        type="button"
        className={`share-import__toggle ${open ? "is-active" : ""}`}
        onClick={() => {
          setOpen((v) => !v);
          setError("");
        }}
      >
        {copy.button}
      </button>
      {open ? (
        <form className="share-import__panel" onSubmit={handleSubmit}>
          <strong className="share-import__title">{copy.title}</strong>
          <p className="share-import__hint">{copy.hint}</p>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={placement === "panel" ? 5 : 6}
            placeholder={copy.placeholder}
          />
          {error ? <p className="share-import__error">{error}</p> : null}
          <div className="share-import__actions">
            <button type="button" className="btn btn--ghost" onClick={() => setOpen(false)}>
              닫기
            </button>
            <button type="submit" className="btn btn--primary" disabled={busy || !text.trim()}>
              {busy ? "해석 중…" : "초안 채우기"}
            </button>
          </div>
        </form>
      ) : null}
    </div>
  );
}
