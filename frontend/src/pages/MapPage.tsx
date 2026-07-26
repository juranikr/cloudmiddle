import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MutableRefObject,
} from "react";
import { MapContainer, Marker, Polygon, TileLayer, Tooltip, useMap, useMapEvents } from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import * as api from "../api";
import type { GeocodeHit, ShareImportResult } from "../api";
import { useAuth } from "../auth";
import { CATEGORY_LIST, CATEGORY_META } from "../categories";
import AddressSearch from "../components/AddressSearch";
import ConfirmBar from "../components/ConfirmBar";
import MapViewPersistence from "../components/MapViewPersistence";
import MarkerPanel, { type CreateDefaults } from "../components/MarkerPanel";
import MessageInbox from "../components/MessageInbox";
import ShareImport from "../components/ShareImport";
import UserLocation, { type LocateStatus } from "../components/UserLocation";
import ZoneDrawer from "../components/ZoneDrawer";
import {
  canUseGeolocation,
  formatGeoError,
  loadLocateOn,
  loadMapView,
  requestLocationAccess,
  saveLocateOn,
} from "../mapStorage";
import { getCategoryIcon, getSearchResultIcon } from "../markerIcons";
import type { LatLng, MarkerCategory, MarkerItem, MarkerPayload, MarkerShape } from "../types";

const JINAN_CENTER: [number, number] = [36.65, 117.12];
const DEFAULT_ZOOM = 12;
const INITIAL_VIEW = loadMapView({ center: JINAN_CENTER, zoom: DEFAULT_ZOOM });

type ToolMode = "pin" | "zone";
type PanelMode = "create" | "view" | "edit" | null;
type DraftKind = "point" | "polygon" | null;
type MobileTab = "map" | "add" | "fav" | "inbox" | "more";

function MapClickHandler({
  enabled,
  onPick,
  ignoreNextClick,
}: {
  enabled: boolean;
  onPick: (lat: number, lng: number) => void;
  ignoreNextClick: MutableRefObject<boolean>;
}) {
  useMapEvents({
    click(e) {
      if (!enabled) return;
      if (ignoreNextClick.current) {
        ignoreNextClick.current = false;
        return;
      }
      onPick(e.latlng.lat, e.latlng.lng);
    },
  });
  return null;
}

function MapVisualFix() {
  const map = useMap();
  useEffect(() => {
    const refresh = () => map.invalidateSize({ animate: false });
    refresh();
    window.addEventListener("orientationchange", refresh);
    window.addEventListener("resize", refresh);
    const t = window.setTimeout(refresh, 200);
    return () => {
      window.removeEventListener("orientationchange", refresh);
      window.removeEventListener("resize", refresh);
      window.clearTimeout(t);
    };
  }, [map]);
  return null;
}

function FlyToPoint({ target, zoom = 16 }: { target: LatLng | null; zoom?: number }) {
  const map = useMap();
  useEffect(() => {
    if (!target) return;
    map.flyTo([target.lat, target.lng], zoom, { duration: 0.75 });
  }, [target, zoom, map]);
  return null;
}

function centroid(points: LatLng[]): LatLng {
  const lat = points.reduce((s, p) => s + p.lat, 0) / points.length;
  const lng = points.reduce((s, p) => s + p.lng, 0) / points.length;
  return { lat, lng };
}

export default function MapPage() {
  const { token, user, logout } = useAuth();
  const [markers, setMarkers] = useState<MarkerItem[]>([]);
  const [categoryFilter, setCategoryFilter] = useState<MarkerCategory | null>(null);
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  const [agentSuggestedOnly, setAgentSuggestedOnly] = useState(false);
  const [toolMode, setToolMode] = useState<ToolMode>("pin");
  const [panelMode, setPanelMode] = useState<PanelMode>(null);
  const [draftKind, setDraftKind] = useState<DraftKind>(null);
  const [draftLatLng, setDraftLatLng] = useState<LatLng | null>(null);
  const [draftPolygon, setDraftPolygon] = useState<LatLng[] | null>(null);
  const [selected, setSelected] = useState<MarkerItem | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [locateOn, setLocateOn] = useState(() => loadLocateOn());
  const [locateSimulate, setLocateSimulate] = useState(() => !canUseGeolocation().ok && loadLocateOn());
  const [locateFollowOnce, setLocateFollowOnce] = useState(false);
  const [locateBusy, setLocateBusy] = useState(false);
  const [locateMsg, setLocateMsg] = useState("");
  const [searchPin, setSearchPin] = useState<(LatLng & { label: string }) | null>(null);
  const [searchHits, setSearchHits] = useState<GeocodeHit[]>([]);
  const [searchError, setSearchError] = useState("");
  const [searchSheetOpen, setSearchSheetOpen] = useState(false);
  const [createDefaults, setCreateDefaults] = useState<CreateDefaults | null>(null);
  const [awaitingImportPick, setAwaitingImportPick] = useState(false);
  const pendingImportPick = useRef(false);
  const [flyTarget, setFlyTarget] = useState<LatLng | null>(null);
  const [inboxOpen, setInboxOpen] = useState(false);
  const [unreadMsg, setUnreadMsg] = useState(0);
  const [mobileTab, setMobileTab] = useState<MobileTab>("map");
  const [moreOpen, setMoreOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [isDesktop, setIsDesktop] = useState(() =>
    typeof window !== "undefined" ? window.matchMedia("(min-width: 860px)").matches : true,
  );

  useEffect(() => {
    const mq = window.matchMedia("(min-width: 860px)");
    const onChange = () => setIsDesktop(mq.matches);
    onChange();
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  const ignoreNextClick = useRef(false);
  const searchIcon = useMemo(() => getSearchResultIcon(), []);

  useEffect(() => {
    if (!token) return;
    void api.fetchUnreadMessageCount(token).then(setUnreadMsg).catch(() => setUnreadMsg(0));
  }, [token]);

  const loadMarkers = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setError("");
    try {
      const data = await api.fetchMarkers(token, {
        category: categoryFilter,
        favoritesOnly,
        agentSuggestedOnly,
      });
      setMarkers(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "목록을 불러오지 못했습니다");
    } finally {
      setLoading(false);
    }
  }, [token, categoryFilter, favoritesOnly, agentSuggestedOnly]);

  useEffect(() => {
    void loadMarkers();
  }, [loadMarkers]);

  const draftIcon = useMemo(
    () =>
      L.divIcon({
        className: "jinan-pin",
        html: `<span class="jinan-pin__dot jinan-pin__dot--draft"></span>`,
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      }),
    [],
  );

  const panelOpen = panelMode !== null;
  const awaitingConfirm = draftKind !== null && !panelOpen;

  function clearDraft() {
    setDraftKind(null);
    setDraftLatLng(null);
    setDraftPolygon(null);
  }

  function clearSearch() {
    setSearchPin(null);
    setSearchHits([]);
    setSearchError("");
    setSearchSheetOpen(false);
  }

  function placePinDraft(lat: number, lng: number, options?: { openCreate?: boolean }) {
    const forceCreate = Boolean(options?.openCreate || pendingImportPick.current);
    if (panelOpen && !forceCreate) return;
    setSelected(null);
    setDraftKind("point");
    setDraftLatLng({ lat, lng });
    setDraftPolygon(null);
    if (forceCreate) {
      pendingImportPick.current = false;
      setAwaitingImportPick(false);
      setPanelMode("create");
    } else {
      setPanelMode(null);
    }
  }

  function placeZoneDraft(points: LatLng[]) {
    if (panelOpen) return;
    setSelected(null);
    setDraftKind("polygon");
    setDraftPolygon(points);
    setDraftLatLng(centroid(points));
    setPanelMode(null);
  }

  function openView(marker: MarkerItem) {
    ignoreNextClick.current = true;
    clearDraft();
    setSearchSheetOpen(false);
    setSelected(marker);
    setPanelMode("view");
    setMobileTab("map");
  }

  function closePanel() {
    setPanelMode(null);
    setSelected(null);
    setCreateDefaults(null);
    pendingImportPick.current = false;
    setAwaitingImportPick(false);
    clearDraft();
    clearSearch();
  }

  function openCreateForm() {
    if (!draftLatLng) return;
    setPanelMode("create");
  }

  async function handleCreate(payload: MarkerPayload) {
    if (!token) return;
    await api.createMarker(token, payload);
    closePanel();
    await loadMarkers();
  }

  async function handleUpdate(id: number, payload: Partial<MarkerPayload>) {
    if (!token) return;
    const updated = await api.updateMarker(token, id, payload);
    setSelected(updated);
    setPanelMode("view");
    await loadMarkers();
  }

  async function handleDelete(id: number) {
    if (!token) return;
    await api.deleteMarker(token, id);
    closePanel();
    await loadMarkers();
  }

  function switchTool(mode: ToolMode) {
    setToolMode(mode);
    clearDraft();
    setPanelMode(null);
    setSelected(null);
    setCreateDefaults(null);
    pendingImportPick.current = false;
    setAwaitingImportPick(false);
    setAddOpen(false);
  }

  function handleSearchResults(hits: GeocodeHit[], err: string) {
    setSearchHits(hits);
    setSearchError(err);
    setSearchSheetOpen(true);
    setPanelMode(null);
    setSelected(null);
    if (hits[0]) {
      setFlyTarget({ lat: hits[0].lat, lng: hits[0].lng });
    }
  }

  function handleSearchPick(hit: GeocodeHit) {
    const point = { lat: hit.lat, lng: hit.lng };
    setSearchPin({ ...point, label: hit.display_name });
    setFlyTarget(point);
    setSearchSheetOpen(false);
    setSelected(null);
    setPanelMode(null);
    clearDraft();
  }

  function handleShareImported(result: ShareImportResult) {
    const hint = result.category_hint as MarkerCategory;
    const category = CATEGORY_LIST.includes(hint)
      ? hint
      : result.source === "dianping"
        ? "restaurant"
        : "other";
    setCreateDefaults({
      title: result.title,
      description: result.description,
      category,
    });
    setToolMode("pin");
    setSelected(null);
    setError(result.note);
    setMoreOpen(false);

    if (result.lat != null && result.lng != null && !result.needs_map_pick) {
      pendingImportPick.current = false;
      setAwaitingImportPick(false);
      const point = { lat: result.lat, lng: result.lng };
      setSearchPin({ ...point, label: result.title });
      setFlyTarget(point);
      placePinDraft(result.lat, result.lng, { openCreate: true });
      return;
    }

    pendingImportPick.current = true;
    setAwaitingImportPick(true);
    clearDraft();
    setPanelMode(null);
  }

  function handleLocateStatus(status: LocateStatus, message: string) {
    setLocateMsg(message);
    if (status === "denied" || status === "error" || status === "outside") {
      setError(message);
      if (status === "denied") {
        setLocateOn(false);
        saveLocateOn(false);
      }
    } else if (status === "watching" && message === "내 위치 추적 중") {
      setError("");
    }
  }

  async function toggleLocate() {
    if (locateOn) {
      setLocateOn(false);
      setLocateSimulate(false);
      saveLocateOn(false);
      setLocateFollowOnce(false);
      setLocateMsg("");
      setError("");
      return;
    }
    const gate = canUseGeolocation();
    if (!gate.ok) {
      setLocateSimulate(true);
      setLocateFollowOnce(false);
      setLocateOn(true);
      saveLocateOn(true);
      setError("");
      setLocateMsg("가상 위치(HTTP 개발용) — 실제 GPS는 HTTPS/localhost에서만 가능");
      return;
    }
    setLocateBusy(true);
    setLocateMsg("위치 권한 요청 중…");
    setError("");
    setLocateSimulate(false);
    try {
      await requestLocationAccess();
      setLocateFollowOnce(true);
      setLocateOn(true);
      saveLocateOn(true);
      setLocateMsg("내 위치 추적 중");
    } catch (err) {
      setError(formatGeoError(err));
      setLocateOn(false);
      saveLocateOn(false);
      setLocateMsg("");
    } finally {
      setLocateBusy(false);
    }
  }

  function onGnb(tab: MobileTab) {
    setMobileTab(tab);
    if (tab === "map") {
      setAddOpen(false);
      setMoreOpen(false);
      setInboxOpen(false);
      if (favoritesOnly) {
        setFavoritesOnly(false);
      }
    } else if (tab === "add") {
      setAddOpen(true);
      setMoreOpen(false);
      setInboxOpen(false);
    } else if (tab === "fav") {
      setFavoritesOnly(true);
      setAgentSuggestedOnly(false);
      setCategoryFilter(null);
      setAddOpen(false);
      setMoreOpen(false);
      setInboxOpen(false);
      setPanelMode(null);
    } else if (tab === "inbox") {
      setInboxOpen(true);
      setAddOpen(false);
      setMoreOpen(false);
    } else if (tab === "more") {
      setMoreOpen(true);
      setAddOpen(false);
      setInboxOpen(false);
    }
  }

  const createShape: MarkerShape = draftKind === "polygon" ? "polygon" : "point";
  const showSearchList = searchSheetOpen && (searchHits.length > 0 || !!searchError);
  const showSearchCard = !!searchPin && !panelOpen && !showSearchList;

  const filterChips = (
    <div className="chips" role="list">
      <button
        type="button"
        className={`chip ${categoryFilter === null && !favoritesOnly && !agentSuggestedOnly ? "is-active" : ""}`}
        onClick={() => {
          setCategoryFilter(null);
          setFavoritesOnly(false);
          setAgentSuggestedOnly(false);
          setMobileTab("map");
        }}
      >
        전체
      </button>
      <button
        type="button"
        className={`chip ${favoritesOnly ? "is-active" : ""}`}
        onClick={() => {
          setFavoritesOnly((v) => !v);
          setAgentSuggestedOnly(false);
          setMobileTab("fav");
        }}
      >
        즐겨찾기
      </button>
      <button
        type="button"
        className={`chip ${agentSuggestedOnly ? "is-active" : ""}`}
        onClick={() => {
          setAgentSuggestedOnly((v) => !v);
          setFavoritesOnly(false);
        }}
      >
        추천
      </button>
      {CATEGORY_LIST.map((c) => (
        <button
          key={c}
          type="button"
          className={`chip ${categoryFilter === c ? "is-active" : ""}`}
          style={{ "--chip-color": CATEGORY_META[c].color } as CSSProperties}
          onClick={() => {
            setCategoryFilter(c);
            setFavoritesOnly(false);
          }}
        >
          <i style={{ background: CATEGORY_META[c].color }} />
          {CATEGORY_META[c].label}
        </button>
      ))}
    </div>
  );

  const toolsBlock = (
    <div className="side-tools">
      <div className="seg seg--tools">
        <button type="button" className={toolMode === "pin" ? "is-active" : ""} onClick={() => switchTool("pin")}>
          핀 찍기
        </button>
        <button type="button" className={toolMode === "zone" ? "is-active" : ""} onClick={() => switchTool("zone")}>
          구역 선택
        </button>
      </div>
      <button
        type="button"
        className={`locate-btn ${locateOn ? "is-active" : ""}`}
        onClick={() => void toggleLocate()}
        disabled={locateBusy}
      >
        {locateBusy ? "요청 중…" : locateOn ? "위치 ON" : "내 위치"}
      </button>
      {token ? (
        <ShareImport token={token} source="amap" placement="main" onImported={handleShareImported} />
      ) : null}
    </div>
  );

  const searchList = showSearchList ? (
    <div className="result-list">
      <div className="result-list__head">
        <strong>검색 결과 {searchHits.length ? `(${searchHits.length})` : ""}</strong>
        <button type="button" className="result-list__close" onClick={clearSearch} aria-label="검색 닫기">
          닫기
        </button>
      </div>
      {searchError ? <p className="result-list__empty">{searchError}</p> : null}
      <ul>
        {searchHits.map((hit) => (
          <li key={`${hit.lat},${hit.lng},${hit.display_name}`}>
            <button type="button" onClick={() => handleSearchPick(hit)}>
              <strong>{hit.display_name.split(",")[0]}</strong>
              <span>{hit.display_name}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  ) : null;

  const searchCard = showSearchCard ? (
    <div className="search-card">
      <div className="search-card__body">
        <strong>{searchPin.label.split(",")[0]}</strong>
        <span>{searchPin.label}</span>
        <span className="search-card__coord">
          {searchPin.lat.toFixed(5)}, {searchPin.lng.toFixed(5)}
        </span>
      </div>
      <div className="search-card__actions">
        <button
          type="button"
          className="btn btn--primary"
          onClick={() => {
            placePinDraft(searchPin.lat, searchPin.lng, { openCreate: true });
          }}
        >
          여기에 등록
        </button>
        <button type="button" className="btn btn--ghost" onClick={clearSearch}>
          닫기
        </button>
      </div>
    </div>
  ) : null;

  const markerPanel = panelMode ? (
    <MarkerPanel
      mode={panelMode === "create" ? "create" : panelMode === "edit" ? "edit" : "view"}
      shape={selected?.shape ?? createShape}
      latlng={draftLatLng}
      polygon={draftPolygon}
      marker={selected}
      createDefaults={createDefaults}
      token={token}
      canEdit={!!selected && !!user}
      onClose={closePanel}
      onCreate={handleCreate}
      onUpdate={handleUpdate}
      onDelete={handleDelete}
      onStartEdit={() => setPanelMode("edit")}
      onMarkerRefresh={(m) => {
        setSelected(m);
        setMarkers((prev) => prev.map((x) => (x.id === m.id ? m : x)));
      }}
    />
  ) : null;

  return (
    <div className={`map-app ${toolMode === "zone" ? "map-app--zone" : ""} ${panelOpen ? "map-app--panel" : ""}`}>
      <aside className="map-side">
        <div className="map-side__top">
          <div className="map-side__brand">
            <div>
              <strong>지난 여행 지도</strong>
              <span>{user?.display_name}</span>
            </div>
            <div className="map-side__brand-actions desktop-only">
              {user?.is_admin ? (
                <a className="link-btn" href="/admin">
                  관리
                </a>
              ) : null}
              <button type="button" className="link-btn" onClick={() => setInboxOpen(true)}>
                메시지{unreadMsg ? ` (${unreadMsg})` : ""}
              </button>
              <button type="button" className="link-btn" onClick={logout}>
                로그아웃
              </button>
            </div>
          </div>
          {token ? <AddressSearch token={token} onResults={handleSearchResults} /> : null}
          {filterChips}
          <div className="desktop-only">{toolsBlock}</div>
          <p className="map-side__hint">
            {awaitingImportPick
              ? "위치를 지도에서 탭하세요 (이름·설명은 자동 입력됩니다)"
              : toolMode === "pin"
                ? "핀: 지도 탭 → 확인 → 입력"
                : "구역을 탭하면 수정 · 길게 그리면 새 구역"}
          </p>
          {locateOn && locateMsg ? <p className="map-side__status">{locateMsg}</p> : null}
          {error ? <p className="map-side__error">{error}</p> : null}
          {loading ? <p className="map-side__status">불러오는 중…</p> : null}
        </div>

        {isDesktop ? (
          <div className="map-side__scroll">
            {searchList}
            {searchCard}
            {markerPanel}
            {!panelOpen && !showSearchList && !showSearchCard ? (
              <div className="place-list">
                <div className="place-list__head">
                  <strong>장소 {markers.length}</strong>
                </div>
                <ul>
                  {markers.map((m) => (
                    <li key={m.id}>
                      <button
                        type="button"
                        onClick={() => {
                          openView(m);
                          setFlyTarget({ lat: m.lat, lng: m.lng });
                        }}
                      >
                        <strong>
                          {m.title}
                          {m.is_agent_suggested ? " · 추천" : ""}
                          {m.is_favorite ? " \u2605" : ""}
                        </strong>
                        <span>{CATEGORY_META[m.category].label}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        ) : null}
      </aside>

      <div className="map-shell">
        <MapContainer
          center={INITIAL_VIEW.center}
          zoom={INITIAL_VIEW.zoom}
          className="map-canvas"
          zoomControl={false}
        >
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
            detectRetina
            maxZoom={19}
            maxNativeZoom={19}
          />
          <MapVisualFix />
          <MapViewPersistence />
          <FlyToPoint target={flyTarget} />
          <UserLocation
            enabled={locateOn}
            simulate={locateSimulate}
            followOnce={locateFollowOnce}
            onFollowed={() => setLocateFollowOnce(false)}
            onStatus={handleLocateStatus}
          />
          <MapClickHandler
            enabled={toolMode === "pin" && !panelOpen}
            onPick={placePinDraft}
            ignoreNextClick={ignoreNextClick}
          />
          <ZoneDrawer
            active={toolMode === "zone" && !panelOpen && !awaitingConfirm}
            zones={markers.filter((m) => m.shape === "polygon" && !!m.polygon)}
            onDrawn={placeZoneDraft}
            onZoneTap={openView}
          />
          {markers.map((m) =>
            m.shape === "polygon" && m.polygon && m.polygon.length >= 3 ? (
              <Polygon
                key={m.id}
                positions={m.polygon.map((p) => [p.lat, p.lng] as [number, number])}
                pathOptions={{
                  color: CATEGORY_META[m.category].color,
                  fillColor: CATEGORY_META[m.category].color,
                  fillOpacity: 0.22,
                  weight: 2,
                }}
                interactive={false}
              >
                <Tooltip permanent direction="center" className="zone-label" opacity={1}>
                  {m.title}
                </Tooltip>
              </Polygon>
            ) : (
              <Marker
                key={m.id}
                position={[m.lat, m.lng]}
                icon={getCategoryIcon(m.category)}
                eventHandlers={{ click: () => openView(m) }}
              />
            ),
          )}
          {searchPin ? (
            <Marker position={[searchPin.lat, searchPin.lng]} icon={searchIcon} title={searchPin.label} />
          ) : null}
          {draftKind === "point" && draftLatLng ? (
            <Marker position={[draftLatLng.lat, draftLatLng.lng]} icon={draftIcon} />
          ) : null}
          {draftKind === "polygon" && draftPolygon ? (
            <Polygon
              positions={draftPolygon.map((p) => [p.lat, p.lng] as [number, number])}
              pathOptions={{ color: "#0f766e", fillColor: "#0f766e", fillOpacity: 0.28, weight: 3 }}
            />
          ) : null}
        </MapContainer>
      </div>

      {!isDesktop ? (
        <div className="mobile-sheets">
          {searchList}
          {searchCard}
          {markerPanel}
        </div>
      ) : null}

      {awaitingConfirm ? (
        <ConfirmBar
          title={draftKind === "polygon" ? "이 구역으로 할까요?" : "이 위치로 할까요?"}
          subtitle={
            draftKind === "polygon"
              ? `꼭짓점 ${draftPolygon?.length ?? 0}개`
              : `${draftLatLng?.lat.toFixed(5)}, ${draftLatLng?.lng.toFixed(5)}`
          }
          onConfirm={openCreateForm}
          onCancel={clearDraft}
        />
      ) : null}

      {addOpen ? (
        <div className="sheet-overlay mobile-only" onClick={() => setAddOpen(false)}>
          <div className="sheet-menu" onClick={(e) => e.stopPropagation()}>
            <strong>지도에 추가</strong>
            <button type="button" onClick={() => switchTool("pin")}>
              핀 찍기
            </button>
            <button type="button" onClick={() => switchTool("zone")}>
              구역 선택
            </button>
            <button type="button" className="btn btn--ghost" onClick={() => setAddOpen(false)}>
              닫기
            </button>
          </div>
        </div>
      ) : null}

      {moreOpen ? (
        <div className="sheet-overlay mobile-only" onClick={() => setMoreOpen(false)}>
          <div className="sheet-menu" onClick={(e) => e.stopPropagation()}>
            <strong>더보기</strong>
            <button type="button" onClick={() => void toggleLocate()} disabled={locateBusy}>
              {locateOn ? "내 위치 끄기" : "내 위치"}
            </button>
            {token ? (
              <ShareImport token={token} source="amap" placement="main" onImported={handleShareImported} />
            ) : null}
            {user?.is_admin ? (
              <a className="sheet-menu__link" href="/admin">
                관리자
              </a>
            ) : null}
            <button type="button" onClick={logout}>
              로그아웃
            </button>
            <button type="button" className="btn btn--ghost" onClick={() => setMoreOpen(false)}>
              닫기
            </button>
          </div>
        </div>
      ) : null}

      <nav className="gnb mobile-only" aria-label="하단 메뉴">
        <button type="button" className={mobileTab === "map" && !favoritesOnly ? "is-active" : ""} onClick={() => onGnb("map")}>
          <span>지도</span>
        </button>
        <button type="button" className={mobileTab === "add" || addOpen ? "is-active" : ""} onClick={() => onGnb("add")}>
          <span>추가</span>
        </button>
        <button type="button" className={favoritesOnly || mobileTab === "fav" ? "is-active" : ""} onClick={() => onGnb("fav")}>
          <span>즐겨찾기</span>
        </button>
        <button
          type="button"
          className={`gnb__msg ${mobileTab === "inbox" || inboxOpen ? "is-active" : ""} ${unreadMsg ? "has-unread" : ""}`}
          onClick={() => onGnb("inbox")}
        >
          <span>메시지{unreadMsg ? ` ${unreadMsg}` : ""}</span>
        </button>
        <button type="button" className={mobileTab === "more" || moreOpen ? "is-active" : ""} onClick={() => onGnb("more")}>
          <span>더보기</span>
        </button>
      </nav>

      {token ? (
        <MessageInbox
          token={token}
          open={inboxOpen}
          onClose={() => {
            setInboxOpen(false);
            setMobileTab("map");
          }}
          onUnreadChange={setUnreadMsg}
          onOpenPlace={(placeId) => {
            const m = markers.find((x) => x.id === placeId);
            if (m) {
              setInboxOpen(false);
              openView(m);
              setFlyTarget({ lat: m.lat, lng: m.lng });
            }
          }}
        />
      ) : null}
    </div>
  );
}
