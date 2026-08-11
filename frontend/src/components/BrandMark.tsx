export default function BrandMark({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`wonrae-brand ${compact ? "wonrae-brand--compact" : ""}`} aria-label="WONRAE 遠來">
      <span className="wonrae-brand__seal" aria-hidden>遠</span>
      <span className="wonrae-brand__word">
        <strong>WONRAE</strong>
        <em>遠來</em>
      </span>
      {!compact ? <small>먼 곳에서 온 친구를 위한 지도 · 有朋自遠方來</small> : null}
    </div>
  );
}
