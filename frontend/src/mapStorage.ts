import type { LatLng } from "./types";

const VIEW_KEY = "jinan_map_view_v1";
const LOCATE_KEY = "jinan_locate_on_v1";

export interface SavedMapView {
  lat: number;
  lng: number;
  zoom: number;
}

export function loadMapView(fallback: { center: [number, number]; zoom: number }): {
  center: [number, number];
  zoom: number;
} {
  try {
    const raw = localStorage.getItem(VIEW_KEY);
    if (!raw) return fallback;
    const v = JSON.parse(raw) as SavedMapView;
    if (
      typeof v.lat === "number" &&
      typeof v.lng === "number" &&
      typeof v.zoom === "number" &&
      v.lat >= -90 &&
      v.lat <= 90 &&
      v.lng >= -180 &&
      v.lng <= 180 &&
      v.zoom >= 3 &&
      v.zoom <= 20
    ) {
      return { center: [v.lat, v.lng], zoom: v.zoom };
    }
  } catch {
    /* ignore */
  }
  return fallback;
}

export function saveMapView(lat: number, lng: number, zoom: number): void {
  try {
    localStorage.setItem(VIEW_KEY, JSON.stringify({ lat, lng, zoom } satisfies SavedMapView));
  } catch {
    /* ignore */
  }
}

export function loadLocateOn(): boolean {
  try {
    return localStorage.getItem(LOCATE_KEY) === "1";
  } catch {
    return false;
  }
}

export function saveLocateOn(on: boolean): void {
  try {
    localStorage.setItem(LOCATE_KEY, on ? "1" : "0");
  } catch {
    /* ignore */
  }
}

export function canUseGeolocation(): { ok: boolean; reason?: string } {
  if (!("geolocation" in navigator)) {
    return { ok: false, reason: "이 브라우저는 위치 기능을 지원하지 않습니다" };
  }
  // iPhone Safari: http://192.168.x.x 에서는 위치 API가 무조건 실패(권한 허용해도 동일)
  if (typeof window !== "undefined" && !window.isSecureContext) {
    const host = window.location.host;
    return {
      ok: false,
      reason: `위치는 HTTPS에서만 동작합니다. 지금 주소는 보안 연결이 아닙니다. 폰에서 https://${host} 로 다시 접속한 뒤(인증서 경고는 ‘방문’ 허용), ‘내 위치’를 눌러 주세요`,
    };
  }
  return { ok: true };
}

export type LocationRequestError = Error & { code?: number; geoCode?: number };

/** iOS: 사용자 탭 핸들러 안에서 호출해야 권한 창이 뜸. HTTPS 필수. */
export function requestLocationAccess(): Promise<GeolocationPosition> {
  return new Promise((resolve, reject) => {
    const gate = canUseGeolocation();
    if (!gate.ok) {
      const err: LocationRequestError = Object.assign(new Error(gate.reason), {
        code: -10,
        geoCode: -10,
      });
      reject(err);
      return;
    }
    navigator.geolocation.getCurrentPosition(resolve, reject, {
      enableHighAccuracy: true,
      timeout: 25000,
      maximumAge: 0,
    });
  });
}

export function formatGeoError(err: unknown): string {
  const gate = canUseGeolocation();
  if (!gate.ok && gate.reason) return gate.reason;

  const code =
    typeof err === "object" && err && "code" in err ? Number((err as { code: number }).code) : -1;

  if (code === 1) {
    return "위치 권한이 거부되었습니다. 아이폰 설정 → 앱 → Safari → 위치 → 허용(또는 웹사이트별 설정) 후 다시 시도하세요. 이미 허용인데도 안 되면 https:// 주소로 접속 중인지 확인하세요";
  }
  if (code === 2) {
    return "위치를 확인할 수 없습니다. 잠시 후 야외·창가에서 다시 시도해 주세요";
  }
  if (code === 3) {
    return "위치 응답이 지연되었습니다. 다시 ‘내 위치’를 눌러 주세요";
  }
  if (code === -10) {
    return (err as Error).message;
  }
  if (err instanceof Error && err.message) return err.message;
  return "위치를 가져오지 못했습니다";
}

export function isLatLng(v: LatLng | null | undefined): v is LatLng {
  return !!v && typeof v.lat === "number" && typeof v.lng === "number";
}
