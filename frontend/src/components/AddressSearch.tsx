import { useState, type FormEvent } from "react";
import * as api from "../api";
import type { GeocodeHit } from "../api";

interface Props {
  token: string;
  cityId: number;
  onResults: (hits: GeocodeHit[], error: string) => void;
  onPlaceIdSearch?: (placeId: number) => Promise<boolean>;
  onQueryChange?: (q: string) => void;
}

export default function AddressSearch({ token, cityId, onResults, onPlaceIdSearch, onQueryChange }: Props) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);

  async function runSearch(e?: FormEvent) {
    e?.preventDefault();
    const query = q.trim();
    if (!query) return;
    setBusy(true);
    try {
      const idMatch = query.match(/^#\s*(\d+)$/);
      if (idMatch && onPlaceIdSearch) {
        const found = await onPlaceIdSearch(Number(idMatch[1]));
        if (!found) onResults([], `현재 도시에서 장소 #${idMatch[1]}을 찾지 못했습니다.`);
        return;
      }
      const data = await api.geocode(token, query, cityId);
      onResults(data, data.length === 0 ? "검색 결과가 없습니다. 지명·영문명을 섞어 보세요." : "");
    } catch (err) {
      onResults([], err instanceof Error ? err.message : "검색 실패");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="search">
      <form className="search__form" onSubmit={(e) => void runSearch(e)}>
        <input
          type="search"
          enterKeyHint="search"
          placeholder="장소·주소·#번호 검색"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            onQueryChange?.(e.target.value);
          }}
        />
        <button type="submit" disabled={busy || !q.trim()}>
          {busy ? "…" : "검색"}
        </button>
      </form>
    </div>
  );
}
