import { useEffect, useRef, useState, type FormEvent } from "react";
import * as api from "../api";
import type { GeocodeHit } from "../api";

interface Props {
  token: string;
  onPick: (hit: GeocodeHit) => void;
}

export default function AddressSearch({ token, onPick }: Props) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<GeocodeHit[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (!boxRef.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  async function runSearch(e?: FormEvent) {
    e?.preventDefault();
    const query = q.trim();
    if (!query) return;
    setBusy(true);
    setError("");
    try {
      const data = await api.geocode(token, query);
      setHits(data);
      setOpen(true);
      if (data.length === 0) setError("검색 결과가 없습니다. 지명·영문명을 섞어 보세요.");
    } catch (err) {
      setHits([]);
      setError(err instanceof Error ? err.message : "검색 실패");
      setOpen(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="search" ref={boxRef}>
      <form className="search__form" onSubmit={runSearch}>
        <input
          type="search"
          enterKeyHint="search"
          placeholder="주소·장소 검색 (예: 趵突泉, Baotu Spring)"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onFocus={() => hits.length > 0 && setOpen(true)}
        />
        <button type="submit" disabled={busy || !q.trim()}>
          {busy ? "…" : "검색"}
        </button>
      </form>
      {open && (hits.length > 0 || error) ? (
        <ul className="search__results">
          {error ? <li className="search__empty">{error}</li> : null}
          {hits.map((hit) => (
            <li key={`${hit.lat},${hit.lng},${hit.display_name}`}>
              <button
                type="button"
                onClick={() => {
                  onPick(hit);
                  setOpen(false);
                }}
              >
                <strong>{hit.display_name.split(",")[0]}</strong>
                <span>{hit.display_name}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
