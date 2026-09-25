"use client";

import { useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { Legend } from "./charts";
import { fmtNumber, fmtSigned } from "@/lib/format";

export type StationDistribution = {
  station_id: string;
  name: string;
  psi: number;
  ks_pvalue: number | null;
  median_ratio: number | null;
  alert: boolean;
  reference: number[];
  current: number[];
};

const AXIS_TICK = { fill: "var(--muted)", fontSize: 11 };

/**
 * Participación de la demanda actual en cada decil de la referencia. Sin drift,
 * cada decil tiene ~10 %; si la demanda sube, la masa se corre a los deciles altos.
 */
export function DistributionCompare({ stations }: { stations: StationDistribution[] }) {
  const sorted = [...stations].sort((a, b) => b.psi - a.psi);
  const [selected, setSelected] = useState(sorted[0]?.station_id ?? "");
  const station = sorted.find((s) => s.station_id === selected) ?? sorted[0];
  if (!station) return null;
  const data = station.reference.map((ref, index) => ({
    decile: `D${index + 1}`,
    reference: ref * 100,
    current: (station.current[index] ?? 0) * 100,
  }));
  return (
    <>
      <div className="controls" style={{ justifyContent: "space-between", marginBottom: 8 }}>
        <Legend
          items={[
            { label: "Referencia (entrenamiento del campeón)", color: "var(--axis)" },
            { label: "Ventana actual (24 h)", color: "var(--series-1)" },
          ]}
        />
        <label>
          <span className="sr-only">Estación</span>
          <select className="select" value={station.station_id} onChange={(event) => setSelected(event.target.value)}>
            {sorted.map((s) => (
              <option key={s.station_id} value={s.station_id}>
                {s.name} · PSI {fmtNumber(s.psi, 2)}
                {s.alert ? " ⚠" : ""}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div style={{ height: 220 }}>
        <ResponsiveContainer>
          <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }} barGap={2} barCategoryGap="22%">
            <CartesianGrid vertical={false} stroke="var(--grid)" />
            <XAxis dataKey="decile" tick={AXIS_TICK} axisLine={{ stroke: "var(--axis)" }} tickLine={false} />
            <YAxis tick={AXIS_TICK} axisLine={false} tickLine={false} width={36} tickFormatter={(v) => `${v} %`} />
            <Tooltip
              cursor={{ fill: "var(--surface-2)" }}
              content={(args) => {
                if (!args.active || !args.payload?.length) return null;
                const p = (args.payload[0] as { payload: (typeof data)[number] }).payload;
                return (
                  <div className="tooltip">
                    <div className="tooltip-title">Decil {p.decile.slice(1)} de log(demanda) de referencia</div>
                    <div className="tooltip-row">
                      <span>Referencia</span>
                      <b>{fmtNumber(p.reference, 1)} %</b>
                    </div>
                    <div className="tooltip-row">
                      <span>Actual</span>
                      <b>{fmtNumber(p.current, 1)} %</b>
                    </div>
                  </div>
                );
              }}
            />
            <Bar dataKey="reference" fill="var(--axis)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
            <Bar dataKey="current" fill="var(--series-1)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="card-foot">
        D1 = demandas más bajas, D10 = más altas, para las mismas horas y tipo de día. {station.name}: PSI {fmtNumber(station.psi, 3)}
        {station.ks_pvalue != null && <> · KS p {station.ks_pvalue < 0.001 ? "< 0,001" : fmtNumber(station.ks_pvalue, 3)}</>}
        {station.median_ratio != null && <> · mediana {fmtSigned((station.median_ratio - 1) * 100, 1)} % frente a la referencia</>}.
      </div>
    </>
  );
}
