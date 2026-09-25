"use client";

import dynamic from "next/dynamic";

import type { MapStation } from "./station-map";

// Leaflet usa `window`: se carga solo en el navegador.
const StationMap = dynamic(() => import("./station-map"), {
  ssr: false,
  loading: () => <div className="map" />,
});

export function StationMapClient({ stations }: { stations: MapStation[] }) {
  return <StationMap stations={stations} />;
}
