import type {
  AdminAgentAction,
  AdminAgentProposal,
  AdminAgentRunHistory,
  AdminAgentTask,
  AdminKnowledge,
  AdminStatus,
  City,
  MarkerItem,
  MarkerPayload,
  PlaceEventItem,
  PlaceNote,
  PlaceChain,
  User,
  UserMessage,
} from "./types";

export interface GeocodeHit {
  query: string;
  display_name: string;
  lat: number;
  lng: number;
  type: string;
  source: string;
  sources: string[];
  confidence: number;
  confidence_label: string;
  storage_allowed: boolean;
  existing_marker_id: number | null;
  external_id: string;
  source_url: string;
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
  opts: { cityId: number; category?: string | null; favoritesOnly?: boolean; agentSuggestedOnly?: boolean },
): Promise<MarkerItem[]> {
  const q = new URLSearchParams();
  q.set("city_id", String(opts.cityId));
  if (opts?.category) q.set("category", opts.category);
  if (opts?.favoritesOnly) q.set("favorites_only", "true");
  if (opts?.agentSuggestedOnly) q.set("agent_suggested_only", "true");
  const qs = q.toString();
  const res = await request(`/api/markers${qs ? `?${qs}` : ""}`, { headers: authHeaders(token) });
  return handle<MarkerItem[]>(res);
}

export async function fetchMarker(token: string, id: number): Promise<MarkerItem> {
  const res = await request(`/api/markers/${id}`, { headers: authHeaders(token) });
  return handle<MarkerItem>(res);
}

export async function fetchCities(token: string): Promise<City[]> {
  const res = await request("/api/cities", { headers: authHeaders(token) });
  return handle<City[]>(res);
}

export async function uploadPlaceImage(
  token: string,
  placeId: number,
  file: File,
): Promise<MarkerItem> {
  const presignRes = await request(`/api/markers/${placeId}/images/presign`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({
      filename: file.name,
      content_type: file.type || "image/jpeg",
    }),
  });
  const presign = await handle<{
    image_id: number;
    upload_url: string;
    public_url: string;
  }>(presignRes);

  const put = await fetch(presign.upload_url, {
    method: "PUT",
    headers: { "Content-Type": file.type || "image/jpeg" },
    body: file,
  });
  if (!put.ok) {
    throw new Error("이미지 업로드에 실패했습니다 (S3)");
  }

  const detail = await request(`/api/markers/${placeId}`, {
    headers: authHeaders(token),
  });
  return handle<MarkerItem>(detail);
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

export async function fetchMarkerEvents(token: string, id: number): Promise<PlaceEventItem[]> {
  const res = await request(`/api/markers/${id}/events`, {
    headers: authHeaders(token),
  });
  return handle<PlaceEventItem[]>(res);
}

export async function geocode(token: string, q: string, cityId: number): Promise<GeocodeHit[]> {
  const params = new URLSearchParams({ q, city_id: String(cityId) });
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
    cityId = 1,
): Promise<ShareImportResult> {
  const res = await request("/api/import/share", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ text, source, city_id: cityId }),
  });
  return handle<ShareImportResult>(res);
}

export async function fetchMessages(token: string): Promise<UserMessage[]> {
  const res = await request("/api/messages", { headers: authHeaders(token) });
  return handle<UserMessage[]>(res);
}

export async function fetchUnreadMessageCount(token: string): Promise<number> {
  const res = await request("/api/messages/unread-count", { headers: authHeaders(token) });
  const data = await handle<{ count: number }>(res);
  return data.count;
}

export async function markMessageRead(token: string, id: number): Promise<UserMessage> {
  const res = await request(`/api/messages/${id}/read`, {
    method: "POST",
    headers: authHeaders(token),
  });
  return handle<UserMessage>(res);
}

export async function markAllMessagesRead(token: string): Promise<void> {
  const res = await request("/api/messages/read-all", {
    method: "POST",
    headers: authHeaders(token),
  });
  await handle<{ marked: number }>(res);
}

export async function createAppeal(
  token: string,
  body: { place_id: number; body: string; message_id?: number | null },
): Promise<void> {
  const res = await request("/api/appeals", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  await handle(res);
}

export async function fetchAdminStatus(token: string): Promise<AdminStatus> {
  const res = await request("/api/admin/status", { headers: authHeaders(token) });
  return handle<AdminStatus>(res);
}

export interface AgentRunResult {
  ok: boolean;
  steps: number;
  message: string;
  unread_before: number;
  unread_after: number;
  city_id: number;
  score: number;
  performance: Record<string, number>;
  remaining_gaps: string[];
  run_id: number | null;
}

export interface AgentRunStatus {
  running: boolean;
  started_at: string | null;
  finished_at: string | null;
  result: AgentRunResult | null;
}

export async function startAdminAgent(
  token: string,
  cityId: number,
  research: boolean,
): Promise<AgentRunStatus> {
  const res = await request("/api/admin/agent/run", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ city_id: cityId, research }),
  });
  return handle(res);
}

export async function fetchAdminAgentStatus(token: string): Promise<AgentRunStatus> {
  const res = await request("/api/admin/agent/run/status", {
    headers: authHeaders(token),
  });
  return handle(res);
}

export async function fetchAdminUsers(token: string): Promise<User[]> {
  const res = await request("/api/admin/users", { headers: authHeaders(token) });
  return handle<User[]>(res);
}

export async function createAdminUser(
  token: string,
  body: { email: string; display_name: string; password: string },
): Promise<User> {
  const res = await request("/api/admin/users", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  return handle<User>(res);
}

export async function updateAdminUser(
  token: string,
  id: number,
  body: { email?: string; display_name?: string; password?: string },
): Promise<User> {
  const res = await request(`/api/admin/users/${id}`, {
    method: "PATCH",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  return handle<User>(res);
}

export async function deleteAdminUser(token: string, id: number): Promise<void> {
  const res = await request(`/api/admin/users/${id}`, {
    method: "DELETE",
    headers: authHeaders(token),
  });
  await handle<void>(res);
}

export async function fetchAdminAgentActions(token: string, cityId: number): Promise<AdminAgentAction[]> {
  const res = await request(`/api/admin/agent/actions?city_id=${cityId}`, { headers: authHeaders(token) });
  return handle<AdminAgentAction[]>(res);
}

export async function rollbackAdminAgentAction(
  token: string,
  eventId: number,
  note = "",
): Promise<{ ok: boolean; rollback_event_id: number; message: string }> {
  const res = await request(`/api/admin/agent/actions/${eventId}/rollback`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ note }),
  });
  return handle(res);
}


export async function fetchAdminKnowledge(token: string): Promise<AdminKnowledge[]> {
  const res = await request("/api/admin/knowledge", { headers: authHeaders(token) });
  return handle<AdminKnowledge[]>(res);
}

export async function fetchAdminAgentRuns(token: string, cityId: number): Promise<AdminAgentRunHistory[]> {
  const res = await request(`/api/admin/agent/runs?city_id=${cityId}`, { headers: authHeaders(token) });
  return handle<AdminAgentRunHistory[]>(res);
}

export async function fetchAdminAgentTasks(token: string, cityId: number): Promise<AdminAgentTask[]> {
  const res = await request(`/api/admin/agent/tasks?city_id=${cityId}&task_status=pending`, { headers: authHeaders(token) });
  return handle<AdminAgentTask[]>(res);
}

export async function fetchPlaceNotes(token: string, placeId: number): Promise<PlaceNote[]> {
  const res = await request(`/api/markers/${placeId}/notes`, { headers: authHeaders(token) });
  return handle<PlaceNote[]>(res);
}

export async function createPlaceNote(
  token: string,
  placeId: number,
  body: string,
  visibility: "shared" | "private" = "shared",
): Promise<PlaceNote> {
  const res = await request(`/api/markers/${placeId}/notes`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ body, visibility }),
  });
  return handle<PlaceNote>(res);
}

export async function deletePlaceNote(token: string, noteId: number): Promise<void> {
  const res = await request(`/api/notes/${noteId}`, {
    method: "DELETE",
    headers: authHeaders(token),
  });
  await handle<void>(res);
}

export async function fetchChains(token: string, cityId?: number): Promise<PlaceChain[]> {
  const q = cityId ? `?city_id=${cityId}` : "";
  const res = await request(`/api/chains${q}`, { headers: authHeaders(token) });
  return handle<PlaceChain[]>(res);
}

export async function createChain(
  token: string,
  body: { name_local: string; name_ko?: string; category?: string; aliases?: string[]; description?: string },
): Promise<PlaceChain> {
  const res = await request("/api/chains", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  return handle<PlaceChain>(res);
}

export async function fetchAdminAgentProposals(
  token: string,
  cityId?: number,
): Promise<AdminAgentProposal[]> {
  const q = new URLSearchParams({ proposal_status: "pending" });
  if (cityId) q.set("city_id", String(cityId));
  const res = await request(`/api/admin/agent/proposals?${q}`, { headers: authHeaders(token) });
  return handle<AdminAgentProposal[]>(res);
}

export async function decideAdminAgentProposal(
  token: string,
  proposalId: number,
  decision: "approve" | "reject",
  note = "",
): Promise<AdminAgentProposal> {
  const res = await request(`/api/admin/agent/proposals/${proposalId}/${decision}`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ note }),
  });
  return handle<AdminAgentProposal>(res);
}

export async function addFavorite(token: string, placeId: number): Promise<{ place_id: number; is_favorite: boolean }> {
  const res = await request(`/api/favorites/${placeId}`, { method: "POST", headers: authHeaders(token) });
  return handle(res);
}

export async function removeFavorite(token: string, placeId: number): Promise<{ place_id: number; is_favorite: boolean }> {
  const res = await request(`/api/favorites/${placeId}`, { method: "DELETE", headers: authHeaders(token) });
  return handle(res);
}
