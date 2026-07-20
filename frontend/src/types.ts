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

export interface MarkerItem {
  id: number;
  user_id: number;
  author_name: string;
  category: MarkerCategory;
  shape: MarkerShape;
  title: string;
  description: string;
  lat: number;
  lng: number;
  polygon: LatLng[] | null;
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
