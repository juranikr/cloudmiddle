import type { MarkerCategory } from "./types";

export const CATEGORY_META: Record<
  MarkerCategory,
  { label: string; color: string }
> = {
  tourist: { label: "관광지", color: "#0d9488" },
  lodging: { label: "숙소", color: "#2563eb" },
  restaurant: { label: "식당", color: "#ea580c" },
  transport: { label: "교통", color: "#7c3aed" },
  shopping: { label: "쇼핑", color: "#db2777" },
  drink: { label: "음료", color: "#ca8a04" },
  convenience: { label: "편의시설", color: "#0891b2" },
  other: { label: "기타", color: "#64748b" },
};

export const CATEGORY_LIST = Object.keys(CATEGORY_META) as MarkerCategory[];
