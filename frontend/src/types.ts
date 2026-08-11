export type MarkerCategory =
  | "tourist"
  | "lodging"
  | "restaurant"
  | "transport"
  | "shopping"
  | "drink"
  | "convenience"
  | "other";

export type MarkerShape = "point" | "polygon";
export type TravelRole =
  | "history"
  | "food"
  | "market_night"
  | "neighborhood"
  | "nature"
  | "shopping"
  | "rest"
  | "practical"
  | "general";

export interface LatLng {
  lat: number;
  lng: number;
}

export interface City {
  id: number;
  slug: string;
  name_ko: string;
  name_local: string;
  country_code: string;
  center_lat: number;
  center_lng: number;
  default_zoom: number;
  search_viewbox: string;
  status: string;
  place_count: number;
  zone_count: number;
}

export interface User {
  id: number;
  email: string;
  display_name: string;
  created_at: string;
  is_admin?: boolean;
}

export interface AdminStatus {
  admin_email: string;
  groq_configured: boolean;
  groq_model: string;
  markers_active: number;
  zones_active?: number;
  events_total: number;
  events_unread: number;
  appeals_open: number;
  users_total: number;
  unread_work_items: number;
  knowledge_topics?: number;
  agent_suggested_places?: number;
  proposals_pending?: number;
}

export interface PlaceEventChange {
  field: string;
  before?: unknown;
  after?: unknown;
}

export interface PlaceEventItem {
  id: number;
  place_id: number | null;
  user_id: number | null;
  actor_name: string;
  actor: string;
  action: string;
  summary: string;
  changes?: PlaceEventChange[];
  groq_read: boolean;
  created_at: string;
}

export interface AdminAgentAction {
  id: number;
  place_id: number | null;
  place_title: string;
  action: string;
  summary: string;
  rolled_back: boolean;
  can_rollback: boolean;
  created_at: string;
}

export interface PlaceImage {
  id: number;
  url: string;
  sort_order: number;
  group_key: string | null;
  content_type: string;
}

export interface PlaceInsight {
  id: number;
  kind: "location" | "history" | "visit" | "tip";
  title: string;
  content: string;
  year_label: string;
  source_url: string;
  source_title: string;
  confidence: number;
  verified_at: string | null;
}

export interface PlaceNote {
  id: number;
  place_id: number;
  user_id: number;
  author_name: string;
  body: string;
  visibility: "shared" | "private";
  is_mine: boolean;
  created_at: string;
  updated_at: string;
}

export interface PlaceChain {
  id: number;
  name_local: string;
  name_ko: string;
  category: string;
  aliases: string[];
  description: string;
  branch_count: number;
  created_at: string;
  updated_at: string;
}

export interface MarkerItem {
  id: number;
  city_id: number;
  user_id: number | null;
  author_name: string;
  contributor_names: string[];
  category: MarkerCategory;
  shape: MarkerShape;
  title: string;
  description: string;
  agent_context: string;
  lat: number;
  lng: number;
  polygon: LatLng[] | null;
  images: PlaceImage[];
  insights: PlaceInsight[];
  zone_id: number | null;
  zone_title: string;
  chain_id: number | null;
  chain_name: string;
  branch_name: string;
  travel_role: TravelRole;
  note_count: number;
  coordinate_source: string;
  coordinate_external_id: string;
  coordinate_query: string;
  coordinate_source_url: string;
  coordinate_confidence: number | null;
  coordinate_crs: string;
  coordinate_verified_at: string | null;
  is_agent_suggested: boolean;
  is_favorite?: boolean;
  created_at: string;
  updated_at: string;
}

export interface MarkerPayload {
  city_id?: number;
  category: MarkerCategory;
  title: string;
  description: string;
  shape: MarkerShape;
  lat: number;
  lng: number;
  polygon?: LatLng[] | null;
  coordinate_source?: string;
  coordinate_external_id?: string;
  coordinate_query?: string;
  coordinate_source_url?: string;
  coordinate_confidence?: number | null;
  coordinate_crs?: string;
  zone_id?: number | null;
  chain_id?: number | null;
  branch_name?: string;
  travel_role?: TravelRole;
}

export interface UserMessage {
  id: number;
  place_id: number | null;
  kind: string;
  title: string;
  body: string;
  read_at: string | null;
  created_at: string;
  can_appeal: boolean;
}


export interface AdminKnowledge {
  id: number;
  topic: string;
  title: string;
  content: string;
  scope?: "global" | "city" | "place";
  city_id?: number | null;
  place_id: number | null;
  category?: string;
  summary?: string;
  principles?: string[];
  next_actions?: string[];
  evidence_count?: number;
  quality_score?: number;
  status?: string;
  version?: number;
  created_at: string;
  updated_at: string;
}

export interface AdminAgentProposal {
  id: number;
  city_id: number;
  place_id: number | null;
  result_place_id: number | null;
  action: "create_place" | "merge_places" | string;
  title: string;
  payload: Record<string, unknown>;
  evidence: string;
  source_urls: string[];
  confidence: number;
  status: string;
  decision_note: string;
  created_at: string;
  decided_at: string | null;
}

export interface AdminAgentRunHistory {
  id: number;
  city_id: number;
  mode: string;
  status: string;
  objective: string;
  score: number;
  metrics: Record<string, unknown>;
  summary: string;
  step_count: number;
  started_at: string;
  finished_at: string | null;
}

export interface AdminAgentTask {
  id: number;
  city_id: number;
  kind: string;
  title: string;
  detail: string;
  success_metric: string;
  priority: number;
  status: string;
  attempts: number;
  result: string;
  created_at: string;
  updated_at: string;
}

export interface TravelPlanItem {
  id: number;
  city_id: number;
  place_id: number;
  day: number;
  slot: "morning" | "afternoon" | "evening";
  sort_order: number;
  note: string;
  place: MarkerItem;
  created_at: string;
  updated_at: string;
}

export interface TravelChatMessage {
  id: number;
  city_id: number;
  role: "user" | "assistant";
  content: string;
  sources: string[];
  place_ids: number[];
  created_at: string;
}
