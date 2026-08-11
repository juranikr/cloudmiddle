import BrandMark from "./BrandMark";

export type WorkspaceView = "map" | "feed" | "plan" | "agent";

const items: Array<{ id: WorkspaceView; icon: string; label: string }> = [
  { id: "map", icon: "圖", label: "지도" },
  { id: "feed", icon: "景", label: "발견" },
  { id: "plan", icon: "程", label: "일정" },
  { id: "agent", icon: "問", label: "여행 대화" },
];

export default function WorkspaceNav({
  value,
  cityLabel,
  onChange,
}: {
  value: WorkspaceView;
  cityLabel: string;
  onChange: (view: WorkspaceView) => void;
}) {
  return (
    <>
      <aside className="workspace-rail desktop-only">
        <BrandMark compact />
        <nav aria-label="주요 화면">
          {items.map((item) => (
            <button
              key={item.id}
              type="button"
              className={value === item.id ? "is-active" : ""}
              onClick={() => onChange(item.id)}
              title={item.label}
            >
              <i>{item.icon}</i><span>{item.label}</span>
            </button>
          ))}
        </nav>
        <small>{cityLabel}</small>
      </aside>
      <nav className="workspace-gnb mobile-only" aria-label="주요 화면">
        {items.map((item) => (
          <button
            key={item.id}
            type="button"
            className={value === item.id ? "is-active" : ""}
            onClick={() => onChange(item.id)}
          >
            <i>{item.icon}</i><span>{item.label}</span>
          </button>
        ))}
      </nav>
    </>
  );
}
