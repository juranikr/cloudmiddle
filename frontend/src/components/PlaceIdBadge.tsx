export default function PlaceIdBadge({ id, className = "" }: { id: number; className?: string }) {
  return (
    <span
      className={`place-id-badge${className ? ` ${className}` : ""}`}
      title={`장소 고유번호 #${id}`}
      aria-label={`장소 번호 ${id}`}
    >
      #{id}
    </span>
  );
}
