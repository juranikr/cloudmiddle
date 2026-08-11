import { useEffect, useMemo, useState } from "react";
import * as api from "../api";
import type { City, MarkerItem, TravelPlanItem } from "../types";
import BrandMark from "./BrandMark";

const SLOT_LABEL = { morning: "아침", afternoon: "낮", evening: "저녁" } as const;

export default function TravelPlanner({ token, city, markers, initialPlace, onOpen }: {
  token: string;
  city: City;
  markers: MarkerItem[];
  initialPlace: MarkerItem | null;
  onOpen: (marker: MarkerItem) => void;
}) {
  const [items, setItems] = useState<TravelPlanItem[]>([]);
  const [day, setDay] = useState(1);
  const [placeId, setPlaceId] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const points = useMemo(() => markers.filter((item) => item.shape === "point"), [markers]);

  useEffect(() => { void api.fetchTravelPlan(token, city.id).then(setItems).catch((e) => setError(e instanceof Error ? e.message : "일정을 불러오지 못했습니다.")); }, [token, city.id]);
  useEffect(() => { if (initialPlace) setPlaceId(initialPlace.id); }, [initialPlace]);

  async function addItem() {
    if (!placeId) return;
    setBusy(true); setError("");
    try {
      const row = await api.addTravelPlanItem(token, city.id, { place_id: placeId, day, slot: "afternoon" });
      setItems((current) => [...current, row]);
      setPlaceId(0);
    } catch (e) { setError(e instanceof Error ? e.message : "일정에 담지 못했습니다."); }
    finally { setBusy(false); }
  }

  async function move(item: TravelPlanItem, next: Partial<Pick<TravelPlanItem, "day" | "slot">>) {
    try {
      const row = await api.updateTravelPlanItem(token, item.id, next);
      setItems((current) => current.map((value) => value.id === row.id ? row : value));
    } catch (e) { setError(e instanceof Error ? e.message : "일정을 옮기지 못했습니다."); }
  }

  async function remove(item: TravelPlanItem) {
    await api.deleteTravelPlanItem(token, item.id);
    setItems((current) => current.filter((value) => value.id !== item.id));
  }

  return (
    <main className="workspace-screen planner">
      <header className="workspace-screen__header"><BrandMark /><div><span>YOUR TWO-DAY ROUTE</span><h1>{city.name_ko}, 내 여행의 리듬</h1><p>아침·낮·저녁 순서로 담고 현장에서 유연하게 바꾸세요.</p></div></header>
      <section className="planner-add">
        <select value={placeId} onChange={(e) => setPlaceId(Number(e.target.value))}><option value={0}>장소를 선택하세요</option>{points.filter((marker) => !items.some((item) => item.place_id === marker.id)).map((marker) => <option key={marker.id} value={marker.id}>{marker.title}</option>)}</select>
        <div><button type="button" className={day === 1 ? "is-active" : ""} onClick={() => setDay(1)}>DAY 1</button><button type="button" className={day === 2 ? "is-active" : ""} onClick={() => setDay(2)}>DAY 2</button></div>
        <button type="button" onClick={() => void addItem()} disabled={!placeId || busy}>{busy ? "담는 중…" : "일정에 담기"}</button>
      </section>
      {error ? <p className="workspace-error">{error}</p> : null}
      <section className="planner-days">
        {[1, 2].map((dayNumber) => (
          <article key={dayNumber}><header><span>DAY {dayNumber}</span><strong>{dayNumber === 1 ? "도시에 인사하기" : "조금 더 깊이 걷기"}</strong></header>
            {(["morning", "afternoon", "evening"] as TravelPlanItem["slot"][]).map((slot) => {
              const slotItems = items.filter((item) => item.day === dayNumber && item.slot === slot);
              return <section className="plan-slot" key={slot}><h2>{SLOT_LABEL[slot]}<span>{slotItems.length || ""}</span></h2>{slotItems.length ? slotItems.map((item) => <div className="plan-item" key={item.id}>
                <button type="button" className="plan-item__place" onClick={() => onOpen(item.place)}>{item.place.images[0]?.url ? <img src={item.place.images[0].url} alt="" /> : <i>{item.place.title.slice(0, 1)}</i>}<span><strong>{item.place.title}</strong><small>{item.place.zone_title || item.place.travel_role}</small></span></button>
                <div><select aria-label="시간대" value={item.slot} onChange={(e) => void move(item, { slot: e.target.value as TravelPlanItem["slot"] })}>{Object.entries(SLOT_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><button type="button" onClick={() => void move(item, { day: dayNumber === 1 ? 2 : 1 })}>D{dayNumber === 1 ? 2 : 1}</button><button type="button" onClick={() => void remove(item)} aria-label="일정에서 빼기">×</button></div>
              </div>) : <p>이 시간은 비워 두었어요.</p>}</section>;
            })}
          </article>
        ))}
      </section>
    </main>
  );
}
