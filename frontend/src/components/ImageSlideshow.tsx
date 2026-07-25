import { useMemo, useState } from "react";
import type { PlaceImage } from "../types";

interface Props {
  images: PlaceImage[];
  onUpload?: (file: File) => Promise<void>;
  canUpload?: boolean;
}

export default function ImageSlideshow({ images, onUpload, canUpload }: Props) {
  const sorted = useMemo(
    () => [...images].filter((i) => i.url).sort((a, b) => a.sort_order - b.sort_order),
    [images],
  );
  const [idx, setIdx] = useState(0);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const current = sorted[Math.min(idx, Math.max(sorted.length - 1, 0))];

  async function handleFile(file: File | undefined) {
    if (!file || !onUpload) return;
    setBusy(true);
    setErr("");
    try {
      await onUpload(file);
      setIdx(sorted.length); // 새 이미지 쪽으로
    } catch (e) {
      setErr(e instanceof Error ? e.message : "업로드 실패");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="slideshow">
      {current ? (
        <div className="slideshow__frame">
          <img src={current.url} alt="" className="slideshow__img" />
          {sorted.length > 1 ? (
            <>
              <button
                type="button"
                className="slideshow__nav slideshow__nav--prev"
                aria-label="이전 사진"
                onClick={() => setIdx((i) => (i - 1 + sorted.length) % sorted.length)}
              >
                ‹
              </button>
              <button
                type="button"
                className="slideshow__nav slideshow__nav--next"
                aria-label="다음 사진"
                onClick={() => setIdx((i) => (i + 1) % sorted.length)}
              >
                ›
              </button>
              <div className="slideshow__dots" aria-hidden>
                {sorted.map((img, i) => (
                  <button
                    key={img.id}
                    type="button"
                    className={`slideshow__dot ${i === idx ? "is-active" : ""}`}
                    onClick={() => setIdx(i)}
                  />
                ))}
              </div>
            </>
          ) : null}
        </div>
      ) : (
        <div className="slideshow__empty">아직 사진이 없습니다</div>
      )}

      {canUpload && onUpload ? (
        <label className={`slideshow__upload ${busy ? "is-busy" : ""}`}>
          <input
            type="file"
            accept="image/jpeg,image/png,image/webp"
            hidden
            disabled={busy}
            onChange={(e) => void handleFile(e.target.files?.[0])}
          />
          {busy ? "업로드 중…" : "사진 추가"}
        </label>
      ) : null}
      {err ? <p className="panel__error">{err}</p> : null}
    </div>
  );
}
