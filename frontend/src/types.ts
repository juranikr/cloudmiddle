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

export interface LatLng {
  lat: number;
  lng: number;
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
  events_total: number;
  events_unread: number;
  appeals_open: number;
  users_total: number;
  unread_work_items: number;
  knowledge_topics?: number;
  agent_suggested_places?: number;
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

export interface MarkerItem {
  id: number;
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
  is_agent_suggested: boolean;
  is_favorite?: boolean;
  created_at: string;
  updated_at: string;
}

export interface MarkerPayload {
  category: MarkerCategory;
  title: string;
  description: string;
  shape: MarkerShape;
  lat: number;
  lng: number;
  polygon?: LatLng[] | null;
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
  place_id: number | null;
  created_at: string;
  updated_at: string;
}
