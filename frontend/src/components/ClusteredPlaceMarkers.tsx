import { useMemo, useState } from "react";
import L from "leaflet";
import { Marker, Popup, useMapEvents } from "react-leaflet";
import { CATEGORY_META } from "../categories";
import { getCategoryIcon } from "../markerIcons";
import type { MarkerItem } from "../types";
import PlaceIdBadge from "./PlaceIdBadge";

interface PlaceCluster {
  lat: number;
  lng: number;
  items: MarkerItem[];
}

interface Props {
  markers: MarkerItem[];
  onSelect: (marker: MarkerItem) => void;
}

const clusterIconCache = new Map<number, L.DivIcon>();

function getClusterIcon(count: number): L.DivIcon {
  const cached = clusterIconCache.get(count);
  if (cached) return cached;
  const size = count >= 100 ? 52 : count >= 10 ? 48 : 44;
  const icon = L.divIcon({
    className: "place-cluster-icon",
    html: `<span aria-hidden="true">${count}</span>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    popupAnchor: [0, -(size / 2 + 4)],
  });
  clusterIconCache.set(count, icon);
  return icon;
}

function clusterMarkers(markers: MarkerItem[], map: L.Map, radius = 48): PlaceCluster[] {
  const clusters: PlaceCluster[] = [];
  const sorted = [...markers].sort((a, b) => a.id - b.id);

  for (const marker of sorted) {
    const point = map.latLngToLayerPoint([marker.lat, marker.lng]);
    let nearest: { cluster: PlaceCluster; distance: number } | null = null;
    for (const cluster of clusters) {
      const center = map.latLngToLayerPoint([cluster.lat, cluster.lng]);
      const distance = point.distanceTo(center);
      if (distance <= radius && (!nearest || distance < nearest.distance)) {
        nearest = { cluster, distance };
      }
    }
    if (!nearest) {
      clusters.push({ lat: marker.lat, lng: marker.lng, items: [marker] });
      continue;
    }
    const cluster = nearest.cluster;
    const count = cluster.items.length;
    cluster.lat = (cluster.lat * count + marker.lat) / (count + 1);
    cluster.lng = (cluster.lng * count + marker.lng) / (count + 1);
    cluster.items.push(marker);
  }

  return clusters;
}

export default function ClusteredPlaceMarkers({ markers, onSelect }: Props) {
  const [mapRevision, setMapRevision] = useState(0);
  const map = useMapEvents({
    zoomend: () => setMapRevision((value) => value + 1),
    resize: () => setMapRevision((value) => value + 1),
  });
  const clusters = useMemo(
    () => clusterMarkers(markers, map),
    [map, mapRevision, markers],
  );

  return clusters.map((cluster) => {
    if (cluster.items.length === 1) {
      const marker = cluster.items[0];
      return (
        <Marker
          key={`place-${marker.id}`}
          position={[marker.lat, marker.lng]}
          icon={getCategoryIcon(marker.category)}
          eventHandlers={{ click: () => onSelect(marker) }}
        />
      );
    }

    const items = [...cluster.items].sort((a, b) => {
      if (!!a.is_favorite !== !!b.is_favorite) return a.is_favorite ? -1 : 1;
      return a.title.localeCompare(b.title, "ko");
    });
    return (
      <Marker
        key={`cluster-${items.map((item) => item.id).join("-")}`}
        position={[cluster.lat, cluster.lng]}
        icon={getClusterIcon(items.length)}
      >
        <Popup className="place-cluster-popup" maxWidth={320} minWidth={240}>
          <div className="cluster-place-picker">
            <strong>이 근처 장소 {items.length}곳</strong>
            <span>열어볼 장소를 선택하세요.</span>
            <div className="cluster-place-picker__list">
              {items.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => {
                    map.closePopup();
                    onSelect(item);
                  }}
                >
                  <i style={{ background: CATEGORY_META[item.category].color }} />
                  <span>
                    <b><PlaceIdBadge id={item.id} />{item.title}</b>
                    <small>
                      {CATEGORY_META[item.category].label}
                      {item.zone_title ? ` · ${item.zone_title}` : ""}
                    </small>
                  </span>
                  {item.is_favorite ? <em aria-label="즐겨찾기">★</em> : null}
                </button>
              ))}
            </div>
          </div>
        </Popup>
      </Marker>
    );
  });
}
