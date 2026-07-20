import { useEffect, useRef } from "react";
import { useMap } from "react-leaflet";
import L from "leaflet";
import { pointInPolygon } from "../geo";
import type { LatLng, MarkerItem } from "../types";

interface Props {
  active: boolean;
  zones: MarkerItem[];
  onDrawn: (points: LatLng[]) => void;
  onZoneTap: (zone: MarkerItem) => void;
}

/** 이 픽셀 이하로만 움직이면 탭(구역 선택), 넘으면 그리기 */
const TAP_SLOP_PX = 14;

function simplify(points: LatLng[], minDist = 0.00008): LatLng[] {
  if (points.length < 2) return points;
  const out: LatLng[] = [points[0]];
  for (let i = 1; i < points.length; i++) {
    const prev = out[out.length - 1];
    const cur = points[i];
    const d = Math.hypot(cur.lat - prev.lat, cur.lng - prev.lng);
    if (d >= minDist) out.push(cur);
  }
  if (out[out.length - 1] !== points[points.length - 1]) {
    out.push(points[points.length - 1]);
  }
  return out;
}

function findZoneAt(point: LatLng, zones: MarkerItem[]): MarkerItem | null {
  // 나중에 그린 구역이 위에 있다고 보고 역순 탐색
  for (let i = zones.length - 1; i >= 0; i--) {
    const z = zones[i];
    if (z.shape === "polygon" && z.polygon && pointInPolygon(point, z.polygon)) {
      return z;
    }
  }
  return null;
}

/**
 * 원인: 지도 dragging이 작은 움직임에도 Leaflet vector click을 취소함.
 * 해결: 탭이면 point-in-polygon으로 구역을 직접 선택.
 */
export default function ZoneDrawer({ active, zones, onDrawn, onZoneTap }: Props) {
  const map = useMap();
  const onDrawnRef = useRef(onDrawn);
  const onZoneTapRef = useRef(onZoneTap);
  const zonesRef = useRef(zones);
  onDrawnRef.current = onDrawn;
  onZoneTapRef.current = onZoneTap;
  zonesRef.current = zones;

  useEffect(() => {
    if (!active) return;

    const container = map.getContainer();
    let tracking = false;
    let drawing = false;
    let startClient: { x: number; y: number } | null = null;
    let startLatLng: LatLng | null = null;
    let points: LatLng[] = [];
    let line: L.Polyline | null = null;
    let dragWasEnabled = true;

    const clearLine = () => {
      if (line) {
        map.removeLayer(line);
        line = null;
      }
    };

    const restoreMapGestures = () => {
      if (dragWasEnabled) map.dragging.enable();
      map.touchZoom.enable();
      map.doubleClickZoom.enable();
      map.scrollWheelZoom.enable();
    };

    const clientXY = (e: MouseEvent | TouchEvent): { x: number; y: number } | null => {
      const point =
        "touches" in e ? e.touches[0] || e.changedTouches[0] : (e as MouseEvent);
      if (!point) return null;
      return { x: point.clientX, y: point.clientY };
    };

    const toLatLng = (e: MouseEvent | TouchEvent): LatLng | null => {
      const point =
        "touches" in e ? e.touches[0] || e.changedTouches[0] : (e as MouseEvent);
      if (!point) return null;
      const rect = container.getBoundingClientRect();
      const ll = map.containerPointToLatLng(
        L.point(point.clientX - rect.left, point.clientY - rect.top),
      );
      return { lat: ll.lat, lng: ll.lng };
    };

    const beginDrawing = (e: MouseEvent | TouchEvent) => {
      drawing = true;
      e.preventDefault();
      clearLine();
      const start = points[0];
      line = L.polyline(start ? [[start.lat, start.lng]] : [], {
        color: "#0f766e",
        weight: 3,
        dashArray: "6 4",
      }).addTo(map);
      map.dragging.disable();
      map.touchZoom.disable();
      map.doubleClickZoom.disable();
      map.scrollWheelZoom.disable();
    };

    const start = (e: MouseEvent | TouchEvent) => {
      if ("button" in e && e.button !== 0) return;
      const xy = clientXY(e);
      const p = toLatLng(e);
      if (!xy || !p) return;

      tracking = true;
      drawing = false;
      startClient = xy;
      startLatLng = p;
      points = [p];
      clearLine();

      // 탭 선택 성공을 위해 드래그로 click이 씹히지 않게 잠시 끔
      dragWasEnabled = map.dragging.enabled();
      map.dragging.disable();
    };

    const move = (e: MouseEvent | TouchEvent) => {
      if (!tracking || !startClient) return;
      const xy = clientXY(e);
      if (!xy) return;

      const dist = Math.hypot(xy.x - startClient.x, xy.y - startClient.y);
      if (!drawing && dist >= TAP_SLOP_PX) {
        beginDrawing(e);
      }
      if (!drawing) return;

      e.preventDefault();
      const p = toLatLng(e);
      if (!p) return;
      points.push(p);
      line?.setLatLngs(points.map((x) => [x.lat, x.lng] as [number, number]));
    };

    const end = (e: MouseEvent | TouchEvent) => {
      if (!tracking) return;
      tracking = false;

      if (!drawing) {
        // 탭 → 좌표가 구역 안이면 선택 (Leaflet click에 의존하지 않음)
        const tapPoint = toLatLng(e) ?? startLatLng;
        startClient = null;
        startLatLng = null;
        points = [];
        restoreMapGestures();
        if (tapPoint) {
          const hit = findZoneAt(tapPoint, zonesRef.current);
          if (hit) onZoneTapRef.current(hit);
        }
        return;
      }

      e.preventDefault();
      drawing = false;
      startClient = null;
      startLatLng = null;
      restoreMapGestures();

      const simplified = simplify(points);
      clearLine();
      points = [];
      if (simplified.length < 3) return;
      onDrawnRef.current(simplified);
    };

    container.style.cursor = "crosshair";
    container.addEventListener("mousedown", start, { passive: true });
    container.addEventListener("mousemove", move, { passive: false });
    container.addEventListener("mouseup", end, { passive: false });
    container.addEventListener("mouseleave", end, { passive: false });
    container.addEventListener("touchstart", start, { passive: true });
    container.addEventListener("touchmove", move, { passive: false });
    container.addEventListener("touchend", end, { passive: false });
    container.addEventListener("touchcancel", end, { passive: false });

    return () => {
      container.style.cursor = "";
      container.removeEventListener("mousedown", start);
      container.removeEventListener("mousemove", move);
      container.removeEventListener("mouseup", end);
      container.removeEventListener("mouseleave", end);
      container.removeEventListener("touchstart", start);
      container.removeEventListener("touchmove", move);
      container.removeEventListener("touchend", end);
      container.removeEventListener("touchcancel", end);
      clearLine();
      restoreMapGestures();
    };
  }, [active, map]);

  return null;
}
