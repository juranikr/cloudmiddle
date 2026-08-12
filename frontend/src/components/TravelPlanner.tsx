import { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../api";
import type { City, MarkerItem, TravelPlan, TravelPlanDay, TravelPlanItem } from "../types";
import BrandMark from "./BrandMark";
import PlaceIdBadge from "./PlaceIdBadge";

function chinaToday(): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}`;
}

function shortTime(value: string | null): string {
  return value ? value.slice(0, 5) : "";
}

function dateLabel(value: string): string {
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Shanghai",
    month: "long",
    day: "numeric",
    weekday: "short",
  }).format(new Date(`${value}T12:00:00+08:00`));
}

function legacyLabel(item: TravelPlanItem): string {
  const slot = { morning: "아침", afternoon: "낮", evening: "저녁" }[item.legacy_slot] ?? item.legacy_slot;
  return item.legacy_day ? `기존 DAY ${item.legacy_day}${slot ? ` · ${slot}` : ""}` : "날짜 미정";
}

export default function TravelPlanner({ token, city, markers, initialPlace, onOpen }: {
  token: string;
  city: City;
  markers: MarkerItem[];
  initialPlace: MarkerItem | null;
  onOpen: (marker: MarkerItem) => void;
}) {
  const [plan, setPlan] = useState<TravelPlan | null>(null);
  const [placeId, setPlaceId] = useState(0);
  const [planDayId, setPlanDayId] = useState(0);
  const [startTime, setStartTime] = useState("");
  const [endTime, setEndTime] = useState("");
  const [itemNote, setItemNote] = useState("");
  const [newDate, setNewDate] = useState(chinaToday);
  const [newDateTitle, setNewDateTitle] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const points = useMemo(() => markers.filter((item) => item.shape === "point"), [markers]);

  const refreshPlan = useCallback(async () => {
    const next = await api.fetchTravelPlan(token, city.id);
    setPlan(next);
    return next;
  }, [token, city.id]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void api.fetchTravelPlan(token, city.id)
      .then((next) => {
        if (cancelled) return;
        setPlan(next);
        if (next.days.length) setPlanDayId((current) => current || next.days[0].id);
      })
      .catch((reason) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "일정표를 불러오지 못했습니다.");
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [token, city.id]);

  useEffect(() => {
    if (initialPlace) setPlaceId(initialPlace.id);
  }, [initialPlace]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void api.fetchTravelPlan(token, city.id).then(setPlan).catch(() => undefined);
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [token, city.id]);

  async function addDate() {
    if (!plan || !newDate || !plan.can_edit) return;
    setBusy(true);
    setError("");
    try {
      const next = await api.addTravelPlanDay(token, plan.id, {
        calendar_date: newDate,
        title: newDateTitle.trim(),
      });
      setPlan(next);
      const added = next.days.find((day) => day.calendar_date === newDate);
      if (added) setPlanDayId(added.id);
      setNewDateTitle("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "날짜를 추가하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  async function addItem() {
    if (!plan || !placeId || !plan.can_edit) return;
    if (endTime && !startTime) {
      setError("종료 시간을 쓰려면 시작 시간도 선택해주세요.");
      return;
    }
    if (startTime && endTime && endTime <= startTime) {
      setError("종료 시간은 시작 시간보다 뒤여야 합니다.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api.addTravelPlanItem(token, plan.id, {
        place_id: placeId,
        plan_day_id: planDayId || null,
        start_time: startTime || null,
        end_time: endTime || null,
        note: itemNote.trim(),
      });
      await refreshPlan();
      setPlaceId(0);
      setStartTime("");
      setEndTime("");
      setItemNote("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "일정에 담지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  async function updateItem(
    item: TravelPlanItem,
    next: Partial<Pick<TravelPlanItem, "plan_day_id" | "start_time" | "end_time" | "note">>,
  ) {
    setError("");
    try {
      await api.updateTravelPlanItem(token, item.id, next);
      await refreshPlan();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "일정을 수정하지 못했습니다.");
    }
  }

  async function removeItem(item: TravelPlanItem) {
    setError("");
    try {
      await api.deleteTravelPlanItem(token, item.id);
      await refreshPlan();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "일정에서 빼지 못했습니다.");
    }
  }

  async function updateDay(day: TravelPlanDay, body: Partial<Pick<TravelPlanDay, "calendar_date" | "title">>) {
    setError("");
    try {
      const next = await api.updateTravelPlanDay(token, day.id, body);
      setPlan(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "날짜를 수정하지 못했습니다.");
    }
  }

  async function removeDay(day: TravelPlanDay) {
    if (!window.confirm(`${dateLabel(day.calendar_date)} 날짜를 지울까요? 장소는 삭제하지 않고 날짜 미정으로 옮깁니다.`)) return;
    setError("");
    try {
      const next = await api.deleteTravelPlanDay(token, day.id);
      setPlan(next);
      if (planDayId === day.id) setPlanDayId(next.days[0]?.id ?? 0);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "날짜를 삭제하지 못했습니다.");
    }
  }

  function renderItems(items: TravelPlanItem[], unscheduled = false) {
    if (!items.length) return <p className="planner-empty">아직 일정이 없습니다.</p>;
    return items.map((item) => (
      <article className="plan-item" key={item.id}>
        <div className="plan-item__time">
          <input
            type="time"
            aria-label={`${item.place.title} 시작 시간`}
            value={shortTime(item.start_time)}
            onChange={(event) => {
              const nextStart = event.target.value || null;
              const nextEnd = nextStart && item.end_time && shortTime(item.end_time) <= nextStart ? null : item.end_time;
              void updateItem(item, { start_time: nextStart, end_time: nextEnd });
            }}
          />
          <span>—</span>
          <input
            type="time"
            aria-label={`${item.place.title} 종료 시간`}
            value={shortTime(item.end_time)}
            onChange={(event) => {
              const value = event.target.value || null;
              if (value && (!item.start_time || value <= shortTime(item.start_time))) {
                setError("종료 시간은 시작 시간보다 뒤여야 합니다.");
                return;
              }
              void updateItem(item, { end_time: value });
            }}
          />
          {unscheduled ? <small>{legacyLabel(item)}</small> : null}
        </div>
        <button type="button" className="plan-item__place" onClick={() => onOpen(item.place)}>
          {item.place.images[0]?.url ? <img src={item.place.images[0].url} alt="" /> : <i>{item.place.title.slice(0, 1)}</i>}
          <span><strong><PlaceIdBadge id={item.place.id} />{item.place.title}</strong><small>{item.place.zone_title || item.place.travel_role}</small></span>
        </button>
        <div className="plan-item__details">
          <select
            aria-label={`${item.place.title} 날짜`}
            value={item.plan_day_id ?? 0}
            onChange={(event) => void updateItem(item, { plan_day_id: Number(event.target.value) || null })}
          >
            <option value={0}>날짜 미정</option>
            {plan?.days.map((day) => <option key={day.id} value={day.id}>{dateLabel(day.calendar_date)}</option>)}
          </select>
          <input
            key={`${item.id}-${item.updated_at}`}
            className="plan-item__note"
            defaultValue={item.note}
            maxLength={1000}
            placeholder="메모"
            onBlur={(event) => {
              const value = event.target.value.trim();
              if (value !== item.note) void updateItem(item, { note: value });
            }}
          />
          <span className="plan-item__author">{item.creator_name} 추가</span>
          <button type="button" className="plan-item__remove" onClick={() => void removeItem(item)} aria-label="일정에서 빼기">×</button>
        </div>
      </article>
    ));
  }

  return (
    <main className="workspace-screen planner">
      <header className="workspace-screen__header">
        <BrandMark />
        <div><span>SHARED ITINERARY</span><h1>{plan?.title || `${city.name_ko} 여행 일정`}</h1><p>날짜와 시간을 자유롭게 정하고, 이 도시를 보는 모든 사용자가 같은 일정표를 함께 편집합니다.</p></div>
      </header>

      {plan ? (
        <section className="planner-share-status">
          <div><strong>도시 공용 일정표</strong><span>게시형 일정표 구조 · 모든 로그인 사용자 편집 가능</span></div>
          <div className="planner-members" aria-label="일정 참여자">
            {plan.members.slice(0, 5).map((member) => <span key={member.user_id} title={`${member.display_name} · ${member.role}`}>{member.display_name.slice(0, 1)}</span>)}
            <small>{plan.members.length}명 참여</small>
          </div>
          <button type="button" onClick={() => void refreshPlan()}>동기화</button>
        </section>
      ) : null}

      <section className="planner-compose">
        <div className="planner-date-add">
          <strong>여행 날짜 추가</strong>
          <label><span>날짜</span><input type="date" value={newDate} onChange={(event) => setNewDate(event.target.value)} /></label>
          <label><span>그날의 제목</span><input value={newDateTitle} maxLength={160} placeholder="예: 시장과 야경" onChange={(event) => setNewDateTitle(event.target.value)} /></label>
          <button type="button" disabled={!plan || busy || !newDate} onClick={() => void addDate()}>날짜 추가</button>
        </div>
        <div className="planner-item-add">
          <strong>장소와 시간 추가</strong>
          <label className="planner-place-field"><span>장소</span><select value={placeId} onChange={(event) => setPlaceId(Number(event.target.value))}><option value={0}>장소를 선택하세요</option>{points.map((marker) => <option key={marker.id} value={marker.id}>#{marker.id} · {marker.title}</option>)}</select></label>
          <label><span>날짜</span><select value={planDayId} onChange={(event) => setPlanDayId(Number(event.target.value))}><option value={0}>날짜 미정</option>{plan?.days.map((day) => <option key={day.id} value={day.id}>{dateLabel(day.calendar_date)}</option>)}</select></label>
          <label><span>시작</span><input type="time" value={startTime} onChange={(event) => setStartTime(event.target.value)} /></label>
          <label><span>종료</span><input type="time" value={endTime} onChange={(event) => setEndTime(event.target.value)} /></label>
          <label className="planner-note-field"><span>메모</span><input value={itemNote} maxLength={1000} placeholder="예약, 메뉴, 만날 곳…" onChange={(event) => setItemNote(event.target.value)} /></label>
          <button type="button" disabled={!plan || !placeId || busy} onClick={() => void addItem()}>{busy ? "처리 중…" : "일정에 추가"}</button>
        </div>
      </section>

      {error ? <p className="workspace-error">{error}</p> : null}
      {loading ? <p className="workspace-empty">공용 일정표를 불러오는 중…</p> : null}

      {plan ? (
        <section className="planner-days">
          {plan.days.map((day) => (
            <article className="planner-day" key={day.id}>
              <header>
                <div><span>{dateLabel(day.calendar_date)}</span><input type="date" value={day.calendar_date} aria-label="날짜 변경" onChange={(event) => void updateDay(day, { calendar_date: event.target.value })} /></div>
                <input className="planner-day__title" key={`${day.id}-${day.updated_at}`} defaultValue={day.title} maxLength={160} placeholder="이날의 제목" onBlur={(event) => { const value = event.target.value.trim(); if (value !== day.title) void updateDay(day, { title: value }); }} />
                <button type="button" onClick={() => void removeDay(day)}>날짜 삭제</button>
              </header>
              <div className="planner-timeline">{renderItems(day.items)}</div>
            </article>
          ))}
          {plan.unscheduled_items.length ? (
            <article className="planner-day planner-day--unscheduled">
              <header><div><span>날짜 미정</span><small>기존 일정과 보류한 장소</small></div></header>
              <div className="planner-timeline">{renderItems(plan.unscheduled_items, true)}</div>
            </article>
          ) : null}
          {!plan.days.length && !plan.unscheduled_items.length ? <p className="workspace-empty">날짜를 추가하고 첫 장소를 함께 담아보세요.</p> : null}
        </section>
      ) : null}
    </main>
  );
}
