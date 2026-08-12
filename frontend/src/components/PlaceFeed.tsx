import { CATEGORY_META } from "../categories";
import type { City, MarkerItem, TravelRole } from "../types";
import BrandMark from "./BrandMark";
import PlaceIdBadge from "./PlaceIdBadge";

const ROLE_LABEL: Record<TravelRole, string> = {
  history: "시간 여행",
  food: "한 끼",
  market_night: "시장과 밤",
  neighborhood: "동네 산책",
  nature: "바깥 풍경",
  shopping: "쇼핑",
  rest: "잠깐의 쉼",
  practical: "여행 실용",
  general: "발견",
};

export default function PlaceFeed({
  city,
  markers,
  onOpen,
  onPlan,
}: {
  city: City;
  markers: MarkerItem[];
  onOpen: (marker: MarkerItem) => void;
  onPlan: (marker: MarkerItem) => void;
}) {
  const points = markers.filter((item) => item.shape === "point");
  return (
    <main className="workspace-screen place-feed">
      <header className="workspace-screen__header">
        <BrandMark />
        <div><span>CURATED CITY NOTES</span><h1>{city.name_ko}에서 만날 장면들</h1><p>지도에 쌓인 장소를 여행의 순간으로 다시 골랐어요.</p></div>
      </header>
      <section className="feed-stories" aria-label="여행 테마">
        {(["food", "market_night", "neighborhood", "nature", "history"] as TravelRole[]).map((role) => {
          const count = points.filter((item) => item.travel_role === role).length;
          return <div key={role}><i>{ROLE_LABEL[role].slice(0, 1)}</i><strong>{ROLE_LABEL[role]}</strong><span>{count}곳</span></div>;
        })}
      </section>
      {points.length ? (
        <section className="feed-grid">
          {points.map((marker, index) => (
            <article key={marker.id} className="feed-card">
              <button type="button" className="feed-card__hero" onClick={() => onOpen(marker)}>
                {marker.images[0]?.url ? <img src={marker.images[0].url} alt="" loading="lazy" /> : (
                  <span className={`feed-card__fallback feed-card__fallback--${index % 4}`}><b>{marker.title.slice(0, 1)}</b><small>{city.name_local}</small></span>
                )}
                <em>{ROLE_LABEL[marker.travel_role ?? "general"]}</em>
                {marker.images.length > 1 ? <small>1 / {marker.images.length}</small> : null}
              </button>
              <div className="feed-card__body">
                <div><span><PlaceIdBadge id={marker.id} />{CATEGORY_META[marker.category].label}{marker.zone_title ? ` · ${marker.zone_title}` : ""}</span>{marker.is_favorite ? <b>★</b> : null}</div>
                <h2>{marker.title}</h2>
                <p>{marker.description || marker.insights[0]?.content || "여행 지도에 저장된 장소입니다."}</p>
                <footer><button type="button" onClick={() => onOpen(marker)}>지도에서 보기</button><button type="button" onClick={() => onPlan(marker)}>일정에 담기</button></footer>
              </div>
            </article>
          ))}
        </section>
      ) : <div className="workspace-empty"><strong>아직 보여드릴 장소가 없어요.</strong><p>지도에서 첫 장소를 추가해 보세요.</p></div>}
    </main>
  );
}
