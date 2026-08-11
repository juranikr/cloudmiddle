import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
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
  const [lightboxOpen, setLightboxOpen] = useState(false);

  const current = sorted[Math.min(idx, Math.max(sorted.length - 1, 0))];

  useEffect(() => {
    if (!lightboxOpen) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setLightboxOpen(false);
      if (event.key === "ArrowLeft") setIdx((i) => (i - 1 + sorted.length) % sorted.length);
      if (event.key === "ArrowRight") setIdx((i) => (i + 1) % sorted.length);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [lightboxOpen, sorted.length]);

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
          <button type="button" className="slideshow__open" onClick={() => setLightboxOpen(true)} aria-label={`사진 크게 보기, ${idx + 1}/${sorted.length}`}>
            <img src={current.url} alt={`장소 사진 ${idx + 1}`} className="slideshow__img" />
          </button>
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
      {lightboxOpen && current ? createPortal(
        <div className="image-lightbox" role="dialog" aria-modal="true" aria-label="장소 사진 전체 화면" onMouseDown={(event) => { if (event.target === event.currentTarget) setLightboxOpen(false); }}>
          <header><span>{idx + 1} / {sorted.length}</span><button type="button" onClick={() => setLightboxOpen(false)} aria-label="사진 닫기">×</button></header>
          <figure><img src={current.url} alt={`장소 사진 ${idx + 1}`} /></figure>
          {sorted.length > 1 ? <><button type="button" className="image-lightbox__nav image-lightbox__nav--prev" onClick={() => setIdx((i) => (i - 1 + sorted.length) % sorted.length)} aria-label="이전 사진">‹</button><button type="button" className="image-lightbox__nav image-lightbox__nav--next" onClick={() => setIdx((i) => (i + 1) % sorted.length)} aria-label="다음 사진">›</button><nav aria-label="사진 선택">{sorted.map((image, imageIndex) => <button key={image.id} type="button" className={imageIndex === idx ? "is-active" : ""} onClick={() => setIdx(imageIndex)} aria-label={`${imageIndex + 1}번 사진`} />)}</nav></> : null}
        </div>,
        document.body,
      ) : null}
    </div>
  );
}
