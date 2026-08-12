import { useEffect, useState, type CSSProperties, type FormEvent } from "react";
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
  PlaceChain,
  PlaceEventItem,
  PlaceNote,
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
  coordinateSource?: string;
  coordinateExternalId?: string;
  coordinateQuery?: string;
  coordinateSourceUrl?: string;
  coordinateConfidence?: number | null;
}

interface Props {
  mode: "create" | "view" | "edit";
  shape: MarkerShape;
  latlng?: LatLng | null;
  polygon?: LatLng[] | null;
  marker?: MarkerItem | null;
  createDefaults?: CreateDefaults | null;
  token?: string | null;
  cityId: number;
  zones: MarkerItem[];
  chains: PlaceChain[];
  canEdit: boolean;
  onClose: () => void;
  onCreate: (payload: MarkerPayload) => Promise<void>;
  onUpdate: (id: number, payload: Partial<MarkerPayload>) => Promise<void>;
  onDelete: (id: number) => Promise<void>;
  onStartEdit: () => void;
  onMarkerRefresh?: (marker: MarkerItem) => void;
  onChainCreated?: (chain: PlaceChain) => void;
}

export default function MarkerPanel({
  mode,
  shape,
  latlng,
  polygon,
  marker,
  createDefaults,
  token,
  cityId,
  zones,
  chains,
  canEdit,
  onClose,
  onCreate,
  onUpdate,
  onDelete,
  onStartEdit,
  onMarkerRefresh,
  onChainCreated,
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
  const [notes, setNotes] = useState<PlaceNote[]>([]);
  const [notesLoading, setNotesLoading] = useState(false);
  const [noteDraft, setNoteDraft] = useState("");
  const [notePrivate, setNotePrivate] = useState(false);
  const [zoneId, setZoneId] = useState("");
  const [chainId, setChainId] = useState("");
  const [branchName, setBranchName] = useState("");
  const [newChainName, setNewChainName] = useState("");

  useEffect(() => {
    if (marker && (mode === "view" || mode === "edit")) {
      setCategory(marker.category);
      setTitle(marker.title);
      setDescription(marker.description);
      setZoneId(marker.zone_id ? String(marker.zone_id) : "");
      setChainId(marker.chain_id ? String(marker.chain_id) : "");
      setBranchName(marker.branch_name ?? "");
    } else if (mode === "create") {
      setCategory(createDefaults?.category ?? "tourist");
      setTitle(createDefaults?.title ?? "");
      setDescription(createDefaults?.description ?? "");
      setZoneId("");
      setChainId("");
      setBranchName("");
    }
    setError("");
    setAppealOpen(false);
    setAppealBody("");
    setAppealDone(false);
    setEvents([]);
    setEventsOpen(false);
    setNewChainName("");
  }, [marker, mode, latlng, polygon, createDefaults]);

  // A note/favorite update replaces the marker object in MapPage while the
  // user is still looking at the same place. Reset note UI only when the
  // actual place (or panel mode) changes, otherwise the freshly created note
  // disappears immediately and the fetch effect below does not rerun because
  // marker.id stayed the same.
  useEffect(() => {
    setNotes([]);
    setNoteDraft("");
    setNotePrivate(false);
  }, [marker?.id, mode]);

  useEffect(() => {
    if (!token || !marker || mode !== "view") return;
    let cancelled = false;
    setNotesLoading(true);
    void api.fetchPlaceNotes(token, marker.id)
      .then((rows) => { if (!cancelled) setNotes(rows); })
      .catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : "메모를 불러오지 못했습니다"); })
      .finally(() => { if (!cancelled) setNotesLoading(false); });
    return () => { cancelled = true; };
  }, [token, marker?.id, mode]);

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

  async function addNote() {
    if (!token || !marker || !noteDraft.trim()) return;
    setBusy(true);
    setError("");
    try {
      const row = await api.createPlaceNote(
        token,
        marker.id,
        noteDraft.trim(),
        notePrivate ? "private" : "shared",
      );
      setNotes((prev) => [...prev, row]);
      setNoteDraft("");
      onMarkerRefresh?.({ ...marker, note_count: marker.note_count + 1 });
    } catch (err) {
      setError(err instanceof Error ? err.message : "메모 저장 실패");
    } finally {
      setBusy(false);
    }
  }

  async function removeNote(note: PlaceNote) {
    if (!token || !marker || !note.is_mine) return;
    try {
      await api.deletePlaceNote(token, note.id);
      setNotes((prev) => prev.filter((item) => item.id !== note.id));
      onMarkerRefresh?.({ ...marker, note_count: Math.max(0, marker.note_count - 1) });
    } catch (err) {
      setError(err instanceof Error ? err.message : "메모 삭제 실패");
    }
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
      let resolvedChainId: number | null = chainId && chainId !== "new" ? Number(chainId) : null;
      if (chainId === "new") {
        if (!token || !newChainName.trim()) {
          throw new Error("새 체인 이름을 입력하세요");
        }
        const created = await api.createChain(token, {
          name_local: newChainName.trim(),
          category,
        });
        resolvedChainId = created.id;
        onChainCreated?.(created);
      }
      if (mode === "create" && latlng) {
        await onCreate({
          category,
          title: title.trim(),
          description: description.trim(),
          shape,
          lat: latlng.lat,
          lng: latlng.lng,
          polygon: shape === "polygon" ? polygon ?? null : null,
          coordinate_source: createDefaults?.coordinateSource ?? "manual",
          coordinate_external_id: createDefaults?.coordinateExternalId ?? "",
          coordinate_query: createDefaults?.coordinateQuery ?? "",
          coordinate_source_url: createDefaults?.coordinateSourceUrl ?? "",
          coordinate_confidence: createDefaults?.coordinateConfidence ?? null,
          coordinate_crs: "WGS84",
          zone_id: !isZone && zoneId ? Number(zoneId) : null,
          chain_id: !isZone ? resolvedChainId : null,
          branch_name: !isZone ? branchName.trim() : "",
        });
      } else if (mode === "edit" && marker) {
        await onUpdate(marker.id, {
          category,
          title: title.trim(),
          description: description.trim(),
          zone_id: !isZone && zoneId ? Number(zoneId) : null,
          chain_id: !isZone ? resolvedChainId : null,
          branch_name: !isZone ? branchName.trim() : "",
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
      <header className={`panel__header${mode === "view" && marker ? " panel__header--place" : ""}`}>
        {mode === "view" && marker ? (
          <div className="panel__heading">
            <span
              className="panel__header-badge"
              style={{ "--place-color": CATEGORY_META[marker.category].color } as CSSProperties}
            >
              {CATEGORY_META[marker.category].label}
              {marker.shape === "polygon" ? " · 구역" : ""}
              {marker.is_agent_suggested ? " · 추천" : ""}
            </span>
            <h2>{marker.title}</h2>
          </div>
        ) : (
          <h2>{heading}</h2>
        )}
        {mode === "view" && marker ? (
          <button
            type="button"
            className={"panel__header-fav" + (marker.is_favorite ? " is-on" : "")}
            onClick={() => void toggleFavorite()}
            aria-label={marker.is_favorite ? "즐겨찾기 해제" : "즐겨찾기"}
            title={marker.is_favorite ? "즐겨찾기 해제" : "즐겨찾기"}
          >
            <span aria-hidden>{marker.is_favorite ? "★" : "☆"}</span>
          </button>
        ) : null}
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
          {marker.zone_title || marker.chain_name ? (
            <div className="place-relations">
              {marker.zone_title ? <span><b>구역</b>{marker.zone_title}</span> : null}
              {marker.chain_name ? (
                <span><b>체인</b>{marker.chain_name}{marker.branch_name ? ` · ${marker.branch_name}` : ""}</span>
              ) : null}
            </div>
          ) : null}
          {marker.insights?.length ? (
            <div className="place-insights">
              {(["location", "history", "visit", "tip"] as const).map((kind) => {
                const rows = marker.insights.filter((item) => item.kind === kind);
                if (!rows.length) return null;
                const labels = {
                  location: "이 장소의 맥락",
                  history: "역사 타임라인",
                  visit: "방문 정보",
                  tip: "현지 팁",
                };
                return (
                  <details key={kind} className={`place-insights__group place-insights__group--${kind}`} open={kind === "location"}>
                    <summary><h4>{labels[kind]}</h4><span>{rows.length}</span></summary>
                    <ul>
                      {rows.map((item) => (
                        <li key={item.id}>
                          <div className="place-insights__title">
                            {item.year_label ? <time>{item.year_label}</time> : null}
                            <strong>{item.title}</strong>
                            <span>{Math.round(item.confidence * 100)}%</span>
                          </div>
                          <p>{linkifyText(item.content)}</p>
                          {item.source_url ? (
                            <a href={item.source_url} target="_blank" rel="noreferrer">
                              출처: {item.source_title || "원문 보기"}
                            </a>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  </details>
                );
              })}
            </div>
          ) : null}
          <div className="coordinate-proof">
            <strong>위치 근거</strong>
            <span>
              {marker.coordinate_source === "manual" ? "지도 직접 지정" : marker.coordinate_source}
              {marker.coordinate_confidence != null
                ? ` · 일치도 ${Math.round(marker.coordinate_confidence * 100)}%`
                : ""}
              {` · ${marker.coordinate_crs}`}
            </span>
            {marker.coordinate_source_url ? (
              <a href={marker.coordinate_source_url} target="_blank" rel="noreferrer">좌표 출처 보기</a>
            ) : null}
          </div>
          {marker.agent_context ? (
            <details className="panel__context">
              <summary><strong>에이전트 정리 메모</strong></summary>
              <p>{linkifyText(marker.agent_context)}</p>
            </details>
          ) : null}
          {token ? (
            <details className="place-notes" open>
              <summary><strong>여행 메모</strong><span>{notes.length || marker.note_count}</span></summary>
              {notesLoading ? <p className="panel__meta">메모 불러오는 중…</p> : null}
              {notes.length ? (
                <ul>
                  {notes.map((note) => (
                    <li key={note.id}>
                      <div><strong>{note.author_name}</strong><small>{note.visibility === "private" ? "나만 보기" : "공유"}</small></div>
                      <p>{linkifyText(note.body)}</p>
                      <time dateTime={note.created_at}>{new Date(note.created_at).toLocaleString("ko-KR")}</time>
                      {note.is_mine ? <button type="button" onClick={() => void removeNote(note)}>삭제</button> : null}
                    </li>
                  ))}
                </ul>
              ) : !notesLoading ? <p className="panel__meta">첫 여행 메모를 남겨보세요.</p> : null}
              <textarea
                value={noteDraft}
                onChange={(e) => setNoteDraft(e.target.value)}
                rows={3}
                maxLength={4000}
                placeholder="개인 일정, 먹어볼 메뉴, 일행에게 남길 팁…"
              />
              <div className="place-notes__compose">
                <label><input type="checkbox" checked={notePrivate} onChange={(e) => setNotePrivate(e.target.checked)} /> 나만 보기</label>
                <button type="button" className="btn btn--primary" disabled={busy || !noteDraft.trim()} onClick={() => void addNote()}>메모 남기기</button>
              </div>
            </details>
          ) : null}
          {token ? (
            <details className="panel__history" onToggle={(e) => {
              if ((e.currentTarget as HTMLDetailsElement).open) setEventsOpen(true);
            }}>
              <summary><strong>시스템 변경 이력</strong></summary>
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
            </details>
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
              cityId={cityId}
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
          {!isZone ? (
            <div className="panel__relation-fields">
              <label>
                소속 구역
                <select value={zoneId} onChange={(e) => setZoneId(e.target.value)}>
                  <option value="">구역 미지정</option>
                  {zones.map((zone) => <option key={zone.id} value={zone.id}>{zone.title}</option>)}
                </select>
              </label>
              <label>
                체인·브랜드
                <select value={chainId} onChange={(e) => setChainId(e.target.value)}>
                  <option value="">독립 장소</option>
                  {chains.map((chain) => (
                    <option key={chain.id} value={chain.id}>
                      {chain.name_local}{chain.name_ko ? ` (${chain.name_ko})` : ""} · {chain.branch_count}개 지점
                    </option>
                  ))}
                  <option value="new">+ 새 체인 만들기</option>
                </select>
              </label>
              {chainId === "new" ? (
                <label>새 체인 이름<input value={newChainName} onChange={(e) => setNewChainName(e.target.value)} maxLength={160} /></label>
              ) : null}
              {chainId ? (
                <label>지점명<input value={branchName} onChange={(e) => setBranchName(e.target.value)} maxLength={120} placeholder="중제점, 선허구점 등" /></label>
              ) : null}
            </div>
          ) : null}
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
