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

export interface GeoBounds {
  south: number;
  north: number;
  west: number;
  east: number;
}

/** API viewbox(west,north,east,south)를 위치 확인용 경계로 변환한다. */
export function parseViewbox(viewbox: string): GeoBounds | null {
  const values = viewbox.split(",").map(Number);
  if (values.length !== 4 || values.some((value) => !Number.isFinite(value))) return null;
  const [west, firstLat, east, secondLat] = values;
  return {
    west: Math.min(west, east),
    east: Math.max(west, east),
    south: Math.min(firstLat, secondLat),
    north: Math.max(firstLat, secondLat),
  };
}

export function isInsideBounds(point: LatLng, bounds: GeoBounds): boolean {
  return (
    point.lat >= bounds.south &&
    point.lat <= bounds.north &&
    point.lng >= bounds.west &&
    point.lng <= bounds.east
  );
}
