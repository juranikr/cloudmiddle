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
  brave_place_configured?: boolean;
  brave_storage_rights?: boolean;
  quality_gaps_suppressed?: number;
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
  keywords?: string[];
  applicability?: Record<string, unknown>;
  source_refs?: string[];
  evidence_count?: number;
  quality_score?: number;
  retrieval_count?: number;
  last_retrieved_at?: string | null;
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

export type AdminAgentOutcomeCategory =
  | "traveler_visible_changed"
  | "proposal_created"
  | "verified_or_waived_no_change"
  | "deferred_or_blocked"
  | "no_yield"
  | "failed";

export interface AdminAgentNextCursor {
  mission_id?: number;
  work_item_id?: number;
  target?: string;
  stage?: string;
  status?: string;
  next_tool?: string;
  wait_reason?: string;
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
  outcome_category: AdminAgentOutcomeCategory;
  material_change_count: number;
  next_work_item_id: number | null;
  next_cursor: AdminAgentNextCursor;
}

export interface AdminAgentRunStep {
  sequence: number;
  phase: string;
  tool: string;
  outcome: "ok" | "changed" | "error" | "repeated" | "no_new_evidence" | string;
  score_delta: number;
  detail: {
    args?: Record<string, unknown>;
    result?: Record<string, unknown> | unknown[];
    progress?: {
      material_change?: boolean;
      new_evidence?: number;
      score?: number;
      no_material_actions?: number;
    };
    [key: string]: unknown;
  };
  created_at: string;
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

export interface AdminAgentMission {
  id: number;
  city_id: number;
  task_id: number | null;
  kind: string;
  title: string;
  objective: string;
  success_metric: string;
  status: string;
  priority: number;
  progress: Record<string, unknown>;
  last_run_id: number | null;
  updated_at: string;
}

export interface AdminAgentWorkItem {
  id: number;
  mission_id: number;
  place_id: number | null;
  target_key: string;
  title: string;
  stage: string;
  status: string;
  state_summary: string;
  next_action: Record<string, unknown>;
  failed_approaches: string[];
  blocked_reason: string;
  retry_condition: string;
  last_run_id: number | null;
  updated_at: string;
}

export interface TravelPlanItem {
  id: number;
  plan_id: number;
  plan_day_id: number | null;
  city_id: number;
  place_id: number;
  created_by_user_id: number;
  creator_name: string;
  day: number;
  slot: "morning" | "afternoon" | "evening" | string;
  start_time: string | null;
  end_time: string | null;
  sort_order: number;
  note: string;
  legacy_day: number | null;
  legacy_slot: string;
  place: MarkerItem;
  created_at: string;
  updated_at: string;
}

export interface TravelPlanDay {
  id: number;
  plan_id: number;
  calendar_date: string;
  title: string;
  note: string;
  sort_order: number;
  created_by_user_id: number | null;
  items: TravelPlanItem[];
  created_at: string;
  updated_at: string;
}

export interface TravelPlanMember {
  user_id: number;
  display_name: string;
  role: "owner" | "editor" | "viewer" | string;
  invitation_status: "accepted" | "invited" | string;
}

export interface TravelPlan {
  id: number;
  city_id: number;
  owner_user_id: number | null;
  owner_name: string;
  title: string;
  description: string;
  visibility: "private" | "shared" | "city_shared" | "public";
  status: "draft" | "published" | "archived";
  timezone: string;
  cover_image_url: string;
  start_date: string | null;
  end_date: string | null;
  can_edit: boolean;
  can_manage: boolean;
  members: TravelPlanMember[];
  days: TravelPlanDay[];
  unscheduled_items: TravelPlanItem[];
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
  candidates: TravelChatCandidate[];
  created_at: string;
}

export interface TravelChatCandidate {
  key: string;
  title: string;
  address: string;
  category: string;
  status: "grounded" | "location_needed" | "located" | "proposed" | "mapped" | string;
  source_urls: string[];
  lat: number | null;
  lng: number | null;
  confidence: number;
  proposal_id: number | null;
}

export interface TravelPreferenceSignal {
  key: string;
  label: string;
  score: number;
  evidence_count: number;
}

export interface TravelAnchor {
  place_id: number;
  title: string;
  lat: number;
  lng: number;
  zone: string;
  sources: string[];
}

export interface PersonalizedPlaceRecommendation {
  place_id: number;
  title: string;
  category: string;
  travel_role: string;
  zone: string;
  score: number;
  reason: string;
  distance_km: number | null;
}

export interface TravelProfile {
  user_id: number;
  city_id: number;
  signals: TravelPreferenceSignal[];
  anchors: TravelAnchor[];
  recommendations: PersonalizedPlaceRecommendation[];
  category_scores: Record<string, number>;
  role_scores: Record<string, number>;
  brand_scores: Record<string, number>;
  favorite_place_ids: number[];
  created_place_ids: number[];
  direct_source_counts: Record<string, number>;
  corrections: Array<Record<string, unknown>>;
  evidence: Record<string, number>;
}
