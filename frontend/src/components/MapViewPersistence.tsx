import { useEffect } from "react";
import { useMap } from "react-leaflet";
import { saveMapView } from "../mapStorage";

/** 지도 이동/줌을 localStorage에 저장해 앱 복귀·새로고침 후에도 유지 */
export default function MapViewPersistence() {
  const map = useMap();

  useEffect(() => {
    const persist = () => {
      const c = map.getCenter();
      saveMapView(c.lat, c.lng, map.getZoom());
    };
    map.on("moveend", persist);
    map.on("zoomend", persist);
    // 초기 뷰도 한 번 저장
    persist();
    return () => {
      map.off("moveend", persist);
      map.off("zoomend", persist);
    };
  }, [map]);

  return null;
}
