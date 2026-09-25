"use client";

import { CircleMarker, MapContainer, TileLayer, Tooltip } from "react-leaflet";

import { ARCHETYPE_LABEL, fmtBias, fmtNumber } from "@/lib/format";
import { useIsDark } from "./shell";

export type MapStation = {
  station_id: string;
  name: string;
  corridor: string;
  archetype: string | null;
  latitude: number;
  longitude: number;
  bias: number | null;
  alert: boolean;
  accuracy: number | null;
};

const CLIP = 0.3;

function markerColor(bias: number | null): string {
  if (bias == null) return "var(--muted)";
  const share = Math.min(1, Math.abs(bias) / CLIP);
  const pole = bias >= 0 ? "var(--div-pos)" : "var(--div-neg)";
  return `color-mix(in oklab, ${pole} ${Math.round(35 + share * 65)}%, var(--div-mid))`;
}

export default function StationMap({ stations }: { stations: MapStation[] }) {
  const dark = useIsDark();
  const lats = stations.map((s) => s.latitude);
  const lons = stations.map((s) => s.longitude);
  const bounds: [[number, number], [number, number]] = [
    [Math.min(...lats), Math.min(...lons)],
    [Math.max(...lats), Math.max(...lons)],
  ];
  return (
    <div className={`map${dark ? " map-dark" : ""}`}>
      <MapContainer
        bounds={bounds}
        boundsOptions={{ padding: [28, 28] }}
        minZoom={10}
        maxZoom={15}
        scrollWheelZoom={false}
        style={{ height: "100%", width: "100%" }}
        attributionControl
      >
        <TileLayer
          url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        />
        {stations.map((station) => (
          <CircleMarker
            key={station.station_id}
            center={[station.latitude, station.longitude]}
            radius={station.alert ? 11 : 8}
            pathOptions={{
              color: station.alert ? (dark ? "#ffffff" : "#0b0b0b") : dark ? "#1a1a19" : "#fcfcfb",
              weight: station.alert ? 2.5 : 2,
              fillColor: markerColor(station.bias),
              fillOpacity: 1,
            }}
          >
            <Tooltip className="station-tip" direction="top" offset={[0, -8]}>
              <div className="tooltip-title">
                {station.name} <span style={{ color: "var(--muted)", fontWeight: 400 }}>· {station.station_id}</span>
              </div>
              <div className="tooltip-row">
                <span>Troncal</span>
                <b>{station.corridor}</b>
              </div>
              <div className="tooltip-row">
                <span>Arquetipo</span>
                <b>{station.archetype ? ARCHETYPE_LABEL[station.archetype] ?? station.archetype : "—"}</b>
              </div>
              <div className="tooltip-row">
                <span>Real vs. perfil</span>
                <b>{fmtBias(station.bias)}</b>
              </div>
              <div className="tooltip-row">
                <span>Accuracy 24 h</span>
                <b>{fmtNumber(station.accuracy, 2)}</b>
              </div>
              <div className="tooltip-row">
                <span>Estado</span>
                <b>{station.alert ? "⚠ Drift de nivel" : "Normal"}</b>
              </div>
            </Tooltip>
          </CircleMarker>
        ))}
      </MapContainer>
    </div>
  );
}
