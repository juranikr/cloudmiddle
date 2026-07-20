import L from "leaflet";
import { CATEGORY_META } from "./categories";
import type { MarkerCategory } from "./types";

const iconCache = new Map<string, L.DivIcon>();

export function getCategoryIcon(category: MarkerCategory, mine: boolean): L.DivIcon {
  const key = `${category}-${mine ? "me" : "other"}`;
  const cached = iconCache.get(key);
  if (cached) return cached;

  const color = CATEGORY_META[category].color;
  const ring = mine ? "#f8fafc" : "rgba(255,255,255,0.85)";
  const border = mine ? color : "rgba(15,23,42,0.15)";

  const icon = L.divIcon({
    className: "jinan-pin",
    html: `<span class="jinan-pin__dot" style="background:${color};box-shadow:0 0 0 2px ${ring},0 2px 8px rgba(15,23,42,.35);border:2px solid ${border}"></span>`,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
    popupAnchor: [0, -12],
  });
  iconCache.set(key, icon);
  return icon;
}

export function getSearchResultIcon(): L.DivIcon {
  return L.divIcon({
    className: "jinan-pin",
    html: `<span class="jinan-pin__dot jinan-pin__dot--search"></span>`,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });
}

export function getMyLocationIcon(): L.DivIcon {
  return L.divIcon({
    className: "jinan-locate",
    html: `<span class="jinan-locate__pulse"></span><span class="jinan-locate__dot"></span>`,
    iconSize: [24, 24],
    iconAnchor: [12, 12],
  });
}
