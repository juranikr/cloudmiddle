import { useState, type FormEvent } from "react";
import * as api from "../api";
import type { GeocodeHit } from "../api";

interface Props {
  token: string;
  cityId: number;
  onResults: (hits: GeocodeHit[], error: string) => void;
  onQueryChange?: (q: string) => void;
}

export default function AddressSearch({ token, cityId, onResults, onQueryChange }: Props) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);

  async function runSearch(e?: FormEvent) {
    e?.preventDefault();
    const query = q.trim();
    if (!query) return;
    setBusy(true);
    try {
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
          placeholder="주소·장소 검색 (예: 趵突泉, Baotu Spring)"
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
