import { useEffect, useState, type FormEvent } from "react";
import * as api from "../api";
import type { ShareImportResult } from "../api";
import { CATEGORY_LIST, CATEGORY_META } from "../categories";
import { linkifyText } from "../linkify";
import type {
  LatLng,
  MarkerCategory,
  MarkerItem,
  MarkerPayload,
  MarkerShape,
  PlaceEventItem,
} from "../types";
import ImageSlideshow from "./ImageSlideshow";
import ShareImport from "./ShareImport";

const ACTION_LABEL: Record<string, string> = {
  create: "추가",
  update: "수정",
  delete: "삭제",
  merge: "병합",
  image_add: "사진 추가",
  image_reorder: "사진 순서",
  context_update: "정리 메모",
  agent_create: "추천 추가",
  appeal: "이의신청",
  rollback: "롤백",
};

const FIELD_LABEL: Record<string, string> = {
  title: "제목",
  description: "설명",
  category: "분류",
  lat: "위도",
  lng: "경도",
  polygon: "구역",
  agent_context: "정리 메모",
  image_ids: "사진 순서",
  image_id: "사진",
  merge: "병합",
};

function truncVal(v: unknown, max = 80): string {
  if (v == null) return "—";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  const one = s.replace(/\s+/g, " ").trim();
  return one.length > max ? `${one.slice(0, max)}…` : one;
}

export interface CreateDefaults {
  title?: string;
  description?: string;
  category?: MarkerCategory;
}

interface Props {
  mode: "create" | "view" | "edit";
  shape: MarkerShape;
  latlng?: LatLng | null;
  polygon?: LatLng[] | null;
  marker?: MarkerItem | null;
  createDefaults?: CreateDefaults | null;
  token?: string | null;
  canEdit: boolean;
  onClose: () => void;
  onCreate: (payload: MarkerPayload) => Promise<void>;
  onUpdate: (id: number, payload: Partial<MarkerPayload>) => Promise<void>;
  onDelete: (id: number) => Promise<void>;
  onStartEdit: () => void;
  onMarkerRefresh?: (marker: MarkerItem) => void;
}

export default function MarkerPanel({
  mode,
  shape,
  latlng,
  polygon,
  marker,
  createDefaults,
  token,
  canEdit,
  onClose,
  onCreate,
  onUpdate,
  onDelete,
  onStartEdit,
  onMarkerRefresh,
}: Props) {
  const [category, setCategory] = useState<MarkerCategory>("tourist");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [appealOpen, setAppealOpen] = useState(false);
  const [appealBody, setAppealBody] = useState("");
  const [appealDone, setAppealDone] = useState(false);
  const [events, setEvents] = useState<PlaceEventItem[]>([]);
  const [eventsOpen, setEventsOpen] = useState(false);
  const [eventsLoading, setEventsLoading] = useState(false);

  useEffect(() => {
    if (marker && (mode === "view" || mode === "edit")) {
      setCategory(marker.category);
      setTitle(marker.title);
      setDescription(marker.description);
    } else if (mode === "create") {
      setCategory(createDefaults?.category ?? "tourist");
      setTitle(createDefaults?.title ?? "");
      setDescription(createDefaults?.description ?? "");
    }
    setError("");
    setAppealOpen(false);
    setAppealBody("");
    setAppealDone(false);
    setEvents([]);
    setEventsOpen(false);
  }, [marker, mode, latlng, polygon, createDefaults]);

  useEffect(() => {
    if (!token || !marker || mode !== "view" || !eventsOpen) return;
    let cancelled = false;
    setEventsLoading(true);
    void api
      .fetchMarkerEvents(token, marker.id)
      .then((rows) => {
        if (!cancelled) setEvents(rows);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "이력을 불러오지 못했습니다");
      })
      .finally(() => {
        if (!cancelled) setEventsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token, marker, mode, eventsOpen]);

  async function submitAppeal() {
    if (!token || !marker || !appealBody.trim()) return;
    setBusy(true);
    setError("");
    try {
      await api.createAppeal(token, { place_id: marker.id, body: appealBody.trim() });
      setAppealDone(true);
      setAppealOpen(false);
      setAppealBody("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "이의신청 실패");
    } finally {
      setBusy(false);
    }
  }

  function applyDianpingDraft(result: ShareImportResult) {
    setTitle(result.title);
    setDescription(result.description);
    const hint = result.category_hint as MarkerCategory;
    if (CATEGORY_LIST.includes(hint)) {
      setCategory(hint);
    } else {
      setCategory("restaurant");
    }
    setError("");
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!title.trim()) {
      setError("제목을 입력하세요");
      return;
    }
    setBusy(true);
    setError("");
    try {
      if (mode === "create" && latlng) {
        await onCreate({
          category,
          title: title.trim(),
          description: description.trim(),
          shape,
          lat: latlng.lat,
          lng: latlng.lng,
          polygon: shape === "polygon" ? polygon ?? null : null,
        });
      } else if (mode === "edit" && marker) {
        await onUpdate(marker.id, {
          category,
          title: title.trim(),
          description: description.trim(),
        });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "저장 실패");
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete() {
    if (!marker) return;
    if (!confirm("이 항목을 삭제할까요?")) return;
    setBusy(true);
    try {
      await onDelete(marker.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "삭제 실패");
      setBusy(false);
    }
  }

  const isZone = (marker?.shape ?? shape) === "polygon";
  const heading =
    mode === "create"
      ? isZone
        ? "새 구역 등록"
        : "새 마커 등록"
      : mode === "edit"
        ? isZone
          ? "구역 수정"
          : "마커 수정"
        : isZone
          ? "구역 정보"
          : "마커 정보";

  async function toggleFavorite() {
    if (!token || !marker || mode === "create") return;
    try {
      const r = marker.is_favorite
        ? await api.removeFavorite(token, marker.id)
        : await api.addFavorite(token, marker.id);
      onMarkerRefresh?.({ ...marker, is_favorite: r.is_favorite });
    } catch {
      /* ignore */
    }
  }


  return (
    <aside className="panel" role="dialog" aria-label={heading}>
      <div className="panel__handle" aria-hidden />
      <header className="panel__header">
        <h2>{heading}</h2>
        <button type="button" className="panel__close" onClick={onClose} aria-label="닫기">
          ×
        </button>
      </header>

      {mode === "view" && marker ? (
        <div className="panel__body">
          <ImageSlideshow
            images={marker.images ?? []}
            canUpload={canEdit && !!token}
            onUpload={
              token
                ? async (file) => {
                    const updated = await api.uploadPlaceImage(token, marker.id, file);
                    onMarkerRefresh?.(updated);
                  }
                : undefined
            }
          />
          <span
            className="panel__badge"
            style={{ background: CATEGORY_META[marker.category].color }}
          >
            {CATEGORY_META[marker.category].label}
            {marker.shape === "polygon" ? " · 구역" : ""}
            {marker.is_agent_suggested ? " · 추천" : ""}
          </span>
          <div className="panel__title-row">
            <h3 className="panel__title">{marker.title}</h3>
            <button
              type="button"
              className={"btn btn--ghost panel__fav" + (marker.is_favorite ? " is-on" : "")}
              onClick={() => void toggleFavorite()}
              aria-label={marker.is_favorite ? "즐겨찾기 해제" : "즐겨찾기"}
              title={marker.is_favorite ? "즐겨찾기 해제" : "즐겨찾기"}
            >
              {marker.is_favorite ? "\u2605" : "\u2606"}
            </button>
          </div>

          <p className="panel__meta">
            {(marker.contributor_names?.length
              ? marker.contributor_names.join(" · ")
              : marker.author_name) || "공유"}
            {marker.shape === "polygon"
              ? ` · 꼭짓점 ${marker.polygon?.length ?? 0}개`
              : ` · ${marker.lat.toFixed(5)}, ${marker.lng.toFixed(5)}`}
          </p>
          <p className="panel__desc">
            {marker.description ? linkifyText(marker.description) : "설명 없음"}
          </p>
          {marker.agent_context ? (
            <div className="panel__context">
              <strong>정리 메모</strong>
              <p>{linkifyText(marker.agent_context)}</p>
            </div>
          ) : null}
          {token ? (
            <div className="panel__history">
              <button
                type="button"
                className="btn btn--ghost"
                onClick={() => setEventsOpen((v) => !v)}
              >
                {eventsOpen ? "이력 닫기" : "변경 이력"}
              </button>
              {eventsOpen ? (
                eventsLoading ? (
                  <p className="panel__meta">이력 불러오는 중…</p>
                ) : events.length === 0 ? (
                  <p className="panel__meta">이력이 없습니다</p>
                ) : (
                  <ul className="panel__history-list">
                    {events.map((ev) => (
                      <li key={ev.id}>
                        <div className="panel__history-top">
                          <strong>{ACTION_LABEL[ev.action] ?? ev.action}</strong>
                          <span>{ev.actor_name}</span>
                          {!ev.groq_read ? <em>에이전트 미확인</em> : null}
                        </div>
                        <p>{ev.summary}</p>
                        {ev.changes && ev.changes.length > 0 ? (
                          <ul className="panel__history-changes">
                            {ev.changes.map((ch, idx) => (
                              <li key={`${ev.id}-${ch.field}-${idx}`}>
                                <span className="panel__history-field">
                                  {FIELD_LABEL[ch.field] ?? ch.field}
                                </span>
                                <span className="panel__history-diff">
                                  {truncVal(ch.before)} → {truncVal(ch.after)}
                                </span>
                              </li>
                            ))}
                          </ul>
                        ) : null}
                        <time dateTime={ev.created_at}>
                          {new Date(ev.created_at).toLocaleString("ko-KR")}
                        </time>
                      </li>
                    ))}
                  </ul>
                )
              ) : null}
            </div>
          ) : null}
          {canEdit ? (
            <div className="panel__actions">
              <button type="button" className="btn btn--ghost" onClick={onStartEdit}>
                수정
              </button>
              <button type="button" className="btn btn--danger" onClick={handleDelete} disabled={busy}>
                삭제
              </button>
            </div>
          ) : null}
          {token ? (
            <div className="panel__appeal">
              {appealDone ? (
                <p className="panel__meta">이의신청을 남겼습니다. 다음 새벽 정리 때 다시 검토합니다.</p>
              ) : appealOpen ? (
                <>
                  <textarea
                    value={appealBody}
                    onChange={(e) => setAppealBody(e.target.value)}
                    rows={3}
                    maxLength={4000}
                    placeholder="병합·추천이 잘못되었거나 보완이 필요하면 내용을 적어 주세요"
                  />
                  <div className="panel__actions">
                    <button type="button" className="btn btn--ghost" onClick={() => setAppealOpen(false)}>
                      취소
                    </button>
                    <button
                      type="button"
                      className="btn btn--primary"
                      disabled={busy || !appealBody.trim()}
                      onClick={() => void submitAppeal()}
                    >
                      이의 남기기
                    </button>
                  </div>
                </>
              ) : (
                <button type="button" className="btn btn--ghost" onClick={() => setAppealOpen(true)}>
                  에이전트 조치에 이의신청
                </button>
              )}
            </div>
          ) : null}
          {error ? <p className="panel__error">{error}</p> : null}
        </div>
      ) : (
        <form className="panel__body panel__form" onSubmit={handleSubmit}>
          {mode === "create" && createDefaults?.title ? (
            <p className="panel__import-banner">
              공유에서 초안을 채웠습니다. <strong>유형</strong>만 확인하고 저장하면 됩니다. (제목·설명은
              필요 시만 수정)
            </p>
          ) : null}
          <p className="panel__meta">
            {isZone
              ? `구역 꼭짓점 ${polygon?.length ?? marker?.polygon?.length ?? 0}개`
              : `위치 ${(latlng ?? marker)!.lat.toFixed(5)}, ${(latlng ?? marker)!.lng.toFixed(5)}`}
          </p>
          {mode === "create" && !isZone && token ? (
            <ShareImport
              token={token}
              source="dianping"
              placement="panel"
              onImported={applyDianpingDraft}
            />
          ) : null}
          <label>
            유형
            <select value={category} onChange={(e) => setCategory(e.target.value as MarkerCategory)}>
              {CATEGORY_LIST.map((c) => (
                <option key={c} value={c}>
                  {CATEGORY_META[c].label}
                </option>
              ))}
            </select>
          </label>
          <label>
            제목
            <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} required />
          </label>
          <label>
            간단한 정보
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              maxLength={2000}
              rows={4}
              placeholder="가는 팁, 영업시간, URL(https://…) 등"
            />
          </label>
          {error ? <p className="panel__error">{error}</p> : null}
          <div className="panel__actions">
            <button type="button" className="btn btn--ghost" onClick={onClose}>
              취소
            </button>
            <button type="submit" className="btn btn--primary" disabled={busy}>
              {busy ? "저장 중…" : mode === "create" && createDefaults?.title ? "초안 저장" : "저장"}
            </button>
          </div>
        </form>
      )}
    </aside>
  );
}
