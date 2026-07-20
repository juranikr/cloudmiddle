import { useEffect, useMemo, useRef, useState } from "react";
import { Circle, Marker, useMap } from "react-leaflet";
import { isInsideJinan } from "../geo";
import { getMyLocationIcon } from "../markerIcons";
import type { LatLng } from "../types";

export type LocateStatus = "idle" | "watching" | "outside" | "denied" | "error";

interface Props {
  enabled: boolean;
  /** HTTP LAN 등 GPS 불가 환경에서 지도 중심을 가상 내 위치로 표시 */
  simulate?: boolean;
  followOnce?: boolean;
  onStatus: (status: LocateStatus, message: string) => void;
  onFollowed?: () => void;
}

const GEO_OPTS: PositionOptions = {
  enableHighAccuracy: true,
  maximumAge: 3000,
  timeout: 20000,
};

export default function UserLocation({
  enabled,
  simulate = false,
  followOnce = false,
  onStatus,
  onFollowed,
}: Props) {
  const map = useMap();
  const [pos, setPos] = useState<(LatLng & { accuracy: number }) | null>(null);
  const watchId = useRef<number | null>(null);
  const icon = useMemo(() => getMyLocationIcon(), []);
  const onStatusRef = useRef(onStatus);
  const onFollowedRef = useRef(onFollowed);
  const followOnceRef = useRef(followOnce);
  onStatusRef.current = onStatus;
  onFollowedRef.current = onFollowed;
  followOnceRef.current = followOnce;

  useEffect(() => {
    if (!enabled) {
      if (watchId.current != null) {
        navigator.geolocation.clearWatch(watchId.current);
        watchId.current = null;
      }
      setPos(null);
      onStatusRef.current("idle", "");
      return;
    }

    // HTTP 등: 실제 GPS 없이 UI 확인용
    if (simulate) {
      const c = map.getCenter();
      const next = { lat: c.lat, lng: c.lng, accuracy: 45 };
      setPos(next);
      onStatusRef.current(
        "watching",
        "가상 위치(개발용) — 아이폰 HTTP에서는 실제 GPS 불가. 지도 중심을 내 위치로 표시 중",
      );
      if (followOnceRef.current) {
        // 이미 지도 중심이라 fly 불필요
        onFollowedRef.current?.();
      }
      return;
    }

    if (!("geolocation" in navigator)) {
      onStatusRef.current("error", "이 브라우저는 위치 기능을 지원하지 않습니다");
      return;
    }

    const applyPosition = (geo: GeolocationPosition) => {
      const next = {
        lat: geo.coords.latitude,
        lng: geo.coords.longitude,
        accuracy: geo.coords.accuracy || 30,
      };
      if (!isInsideJinan(next)) {
        setPos(null);
        onStatusRef.current(
          "outside",
          "현재 위치가 지난(济南) 지도 범위 밖이라 표시하지 않습니다",
        );
        return;
      }
      setPos(next);
      onStatusRef.current("watching", "내 위치 추적 중");
      if (followOnceRef.current) {
        map.flyTo([next.lat, next.lng], Math.max(map.getZoom(), 15), { duration: 0.8 });
        onFollowedRef.current?.();
      }
    };

    const onError = (err: GeolocationPositionError) => {
      setPos(null);
      if (!window.isSecureContext) {
        onStatusRef.current(
          "error",
          "실제 GPS는 HTTPS 또는 PC의 localhost에서만 됩니다",
        );
        return;
      }
      if (err.code === err.PERMISSION_DENIED) {
        onStatusRef.current(
          "denied",
          "위치 권한이 거부되었습니다. 설정 → Safari → 위치 → 허용 후 다시 시도하세요",
        );
      } else if (err.code === err.TIMEOUT) {
        onStatusRef.current("error", "위치 응답이 지연되었습니다. 다시 ‘내 위치’를 눌러 주세요");
      } else {
        onStatusRef.current("error", "위치를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요");
      }
    };

    const startWatch = () => {
      if (watchId.current != null) {
        navigator.geolocation.clearWatch(watchId.current);
        watchId.current = null;
      }
      onStatusRef.current("watching", "위치 확인 중…");
      watchId.current = navigator.geolocation.watchPosition(applyPosition, onError, GEO_OPTS);
    };

    startWatch();

    const onVisible = () => {
      if (document.visibilityState === "visible") startWatch();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("pageshow", onVisible);

    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("pageshow", onVisible);
      if (watchId.current != null) {
        navigator.geolocation.clearWatch(watchId.current);
        watchId.current = null;
      }
    };
  }, [enabled, simulate, map]);

  if (!pos) return null;

  return (
    <>
      <Circle
        center={[pos.lat, pos.lng]}
        radius={Math.min(Math.max(pos.accuracy, 12), 120)}
        pathOptions={{
          color: simulate ? "#0f766e" : "#2563eb",
          fillColor: simulate ? "#14b8a6" : "#3b82f6",
          fillOpacity: 0.15,
          weight: 1,
        }}
      />
      <Marker position={[pos.lat, pos.lng]} icon={icon} interactive={false} />
    </>
  );
}
