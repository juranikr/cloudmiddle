import type { MarkerItem, MarkerPayload, User } from "./types";

export interface GeocodeHit {
  display_name: string;
  lat: number;
  lng: number;
  type: string;
}

/** 개발: Vite 프록시(/api). 배포: 같은 오리진(ALB)의 /api */
function getApiBase(): string {
  if (import.meta.env.VITE_API_URL) return import.meta.env.VITE_API_URL.replace(/\/$/, "");
  return "";
}

const API_URL = getApiBase();

function authHeaders(token: string | null): HeadersInit {
  const headers: HeadersInit = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(`${API_URL}${path}`, init);
  } catch {
    throw new Error(
      "서버에 연결하지 못했습니다. PC에서 API(8000)·프론트(5173)가 켜져 있는지, 같은 Wi-Fi인지 확인하세요.",
    );
  }
}

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = "요청에 실패했습니다";
    try {
      const data = await res.json();
      detail = data.detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export async function login(email: string, password: string): Promise<string> {
  const res = await request("/api/auth/login", {
    method: "POST",
    headers: authHeaders(null),
    body: JSON.stringify({ email, password }),
  });
  const data = await handle<{ access_token: string }>(res);
  return data.access_token;
}

export async function fetchMe(token: string): Promise<User> {
  const res = await request("/api/auth/me", {
    headers: authHeaders(token),
  });
  return handle<User>(res);
}

export async function fetchMarkers(
  token: string,
  opts: { mine?: boolean; category?: string | null },
): Promise<MarkerItem[]> {
  const params = new URLSearchParams();
  if (opts.mine) params.set("mine", "true");
  if (opts.category) params.set("category", opts.category);
  const qs = params.toString();
  const res = await request(`/api/markers${qs ? `?${qs}` : ""}`, {
    headers: authHeaders(token),
  });
  return handle<MarkerItem[]>(res);
}

export async function createMarker(token: string, body: MarkerPayload): Promise<MarkerItem> {
  const res = await request("/api/markers", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  return handle<MarkerItem>(res);
}

export async function updateMarker(
  token: string,
  id: number,
  body: Partial<MarkerPayload>,
): Promise<MarkerItem> {
  const res = await request(`/api/markers/${id}`, {
    method: "PATCH",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  return handle<MarkerItem>(res);
}

export async function deleteMarker(token: string, id: number): Promise<void> {
  const res = await request(`/api/markers/${id}`, {
    method: "DELETE",
    headers: authHeaders(token),
  });
  await handle<void>(res);
}

export async function geocode(token: string, q: string): Promise<GeocodeHit[]> {
  const params = new URLSearchParams({ q });
  const res = await request(`/api/geocode?${params}`, {
    headers: authHeaders(token),
  });
  return handle<GeocodeHit[]>(res);
}

export interface ShareImportResult {
  source: string;
  title: string;
  description: string;
  address: string;
  source_url: string;
  lat: number | null;
  lng: number | null;
  category_hint: string;
  needs_map_pick: boolean;
  note: string;
}

export async function importShare(
  token: string,
  text: string,
  source: "amap" | "dianping" | "" = "",
): Promise<ShareImportResult> {
  const res = await request("/api/import/share", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ text, source }),
  });
  return handle<ShareImportResult>(res);
}
