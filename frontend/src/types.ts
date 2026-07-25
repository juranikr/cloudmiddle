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
