import type { LatLng } from "./types";

/** ray casting */
export function pointInPolygon(point: LatLng, polygon: LatLng[]): boolean {
  if (polygon.length < 3) return false;
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const xi = polygon[i].lng;
    const yi = polygon[i].lat;
    const xj = polygon[j].lng;
    const yj = polygon[j].lat;
    const intersect =
      yi > point.lat !== yj > point.lat &&
      point.lng < ((xj - xi) * (point.lat - yi)) / (yj - yi + 0.0) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}

/** 지난시 대략 범위 — 이 안일 때만 내 위치 표시 */
export const JINAN_BOUNDS = {
  south: 36.35,
  north: 36.95,
  west: 116.7,
  east: 117.55,
};

export function isInsideJinan(point: LatLng): boolean {
  return (
    point.lat >= JINAN_BOUNDS.south &&
    point.lat <= JINAN_BOUNDS.north &&
    point.lng >= JINAN_BOUNDS.west &&
    point.lng <= JINAN_BOUNDS.east
  );
}
