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

/** 레이아웃/회전 후 타일 깨짐 방지 + retina 타일 선명도 */
function MapVisualFix() {
  const map = useMap();

  useEffect(() => {
    const refresh = () => {
      map.invalidateSize({ animate: false });
    };
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
  const [createDefaults, setCreateDefaults] = useState<CreateDefaults | null>(null);
  const [awaitingImportPick, setAwaitingImportPick] = useState(false);
  const pendingImportPick = useRef(false);
  const [flyTarget, setFlyTarget] = useState<LatLng | null>(null);
  const ignoreNextClick = useRef(false);
  const searchIcon = useMemo(() => getSearchResultIcon(), []);

  const loadMarkers = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setError("");
    try {
      const data = await api.fetchMarkers(token, {
        category: categoryFilter,
      });
      setMarkers(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "목록을 불러오지 못했습니다");
    } finally {
      setLoading(false);
    }
  }, [token, categoryFilter]);

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
    setSelected(marker);
    setPanelMode("view");
  }

  function closePanel() {
    setPanelMode(null);
    setSelected(null);
    setCreateDefaults(null);
    pendingImportPick.current = false;
    setAwaitingImportPick(false);
    clearDraft();
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
  }

  function handleSearchPick(hit: GeocodeHit) {
    const point = { lat: hit.lat, lng: hit.lng };
    setSearchPin({ ...point, label: hit.display_name });
    setFlyTarget(point);
    if (toolMode === "pin" && !panelOpen) {
      placePinDraft(hit.lat, hit.lng);
    }
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
    // 아이폰 + http://192.168… : OS가 GPS를 막음 → 개발용 가상 위치로 대체
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

  const createShape: MarkerShape = draftKind === "polygon" ? "polygon" : "point";

  return (
    <div className={`map-app ${toolMode === "zone" ? "map-app--zone" : ""}`}>
      <header className="topbar">
        <div className="topbar__brand">
          <strong>지난 여행 지도</strong>
          <span>{user?.display_name}</span>
        </div>
        <div className="topbar__filters">
          <button type="button" className="topbar__logout" onClick={logout}>
            로그아웃
          </button>
        </div>

        <div className="topbar__tools-row">
          <div className="seg seg--tools">
            <button
              type="button"
              className={toolMode === "pin" ? "is-active" : ""}
              onClick={() => switchTool("pin")}
            >
              핀 찍기
            </button>
            <button
              type="button"
              className={toolMode === "zone" ? "is-active" : ""}
              onClick={() => switchTool("zone")}
            >
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
        </div>

        {token ? (
          <>
            <AddressSearch token={token} onPick={handleSearchPick} />
            <ShareImport
              token={token}
              source="amap"
              placement="main"
              onImported={handleShareImported}
            />
          </>
        ) : null}

        <div className="chips" role="list">
          <button
            type="button"
            className={`chip ${categoryFilter === null ? "is-active" : ""}`}
            onClick={() => setCategoryFilter(null)}
          >
            모든 유형
          </button>
          {CATEGORY_LIST.map((c) => (
            <button
              key={c}
              type="button"
              className={`chip ${categoryFilter === c ? "is-active" : ""}`}
              style={{ "--chip-color": CATEGORY_META[c].color } as CSSProperties}
              onClick={() => setCategoryFilter(c)}
            >
              <i style={{ background: CATEGORY_META[c].color }} />
              {CATEGORY_META[c].label}
            </button>
          ))}
        </div>
        <p className="topbar__hint">
          {awaitingImportPick
            ? "위치를 지도에서 탭하세요 (이름·설명은 자동 입력됩니다)"
            : toolMode === "pin"
              ? "핀: 지도 탭 → 입력. 따종은 등록 화면에서 초안 만들기 · 고덕은 위 버튼"
              : "구역을 탭하면 내용 수정 · 손가락으로 길게 그리면 새 구역 생성"}
        </p>
        {locateOn && locateMsg ? <p className="topbar__status">{locateMsg}</p> : null}
        {error ? <p className="topbar__error">{error}</p> : null}
        {loading ? <p className="topbar__status">불러오는 중…</p> : null}
      </header>

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
                <Tooltip
                  permanent
                  direction="center"
                  className="zone-label"
                  opacity={1}
                >
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
            <Marker
              position={[searchPin.lat, searchPin.lng]}
              icon={searchIcon}
              title={searchPin.label}
            />
          ) : null}
          {draftKind === "point" && draftLatLng ? (
            <Marker position={[draftLatLng.lat, draftLatLng.lng]} icon={draftIcon} />
          ) : null}
          {draftKind === "polygon" && draftPolygon ? (
            <Polygon
              positions={draftPolygon.map((p) => [p.lat, p.lng] as [number, number])}
              pathOptions={{
                color: "#0f766e",
                fillColor: "#0f766e",
                fillOpacity: 0.28,
                weight: 3,
              }}
            />
          ) : null}
        </MapContainer>
      </div>

      {awaitingConfirm ? (
        <ConfirmBar
          title={draftKind === "polygon" ? "이 구역으로 할까요?" : "이 위치로 할까요?"}
          subtitle={
            draftKind === "polygon"
              ? `꼭짓점 ${draftPolygon?.length ?? 0}개 · 지도를 가리지 않고 확인 후 입력`
              : `${draftLatLng?.lat.toFixed(5)}, ${draftLatLng?.lng.toFixed(5)}`
          }
          onConfirm={openCreateForm}
          onCancel={clearDraft}
        />
      ) : null}

      {panelMode ? (
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
      ) : null}
    </div>
  );
}
