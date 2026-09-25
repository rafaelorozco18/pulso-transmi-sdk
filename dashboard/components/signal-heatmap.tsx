"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

import { fmtBias, fmtDateTime, fmtNumber, fmtSigned, fmtTick } from "@/lib/format";

export type HeatCell = {
  station_id: string;
  ts: number;
  value: number;
  alert: boolean;
  /** Dato secundario para el tooltip (p. ej. p-valor KS). */
  extra?: number | null;
};
export type HeatRow = { station_id: string; name: string };

/**
 * - bias: divergente azul ↔ rojo con punto medio gris (satura en ±0,3 de log).
 * - psi / shape: secuencial de un solo tono, de "cerca de cero" a "máximo".
 */
export type HeatKind = "bias" | "psi" | "shape";

const SCALE: Record<HeatKind, { max: number; label: string }> = {
  bias: { max: 0.3, label: "log(real / perfil)" },
  psi: { max: 0.5, label: "PSI" },
  shape: { max: 0.2, label: "Distancia de forma" },
};

function cellColor(kind: HeatKind, value: number): string {
  const share = Math.min(1, Math.abs(value) / SCALE[kind].max);
  if (kind === "bias") {
    const pole = value >= 0 ? "var(--div-pos)" : "var(--div-neg)";
    return `color-mix(in oklab, ${pole} ${Math.round(share * 100)}%, var(--div-mid))`;
  }
  return `color-mix(in oklab, var(--seq-hi) ${Math.round(share * 100)}%, var(--seq-lo))`;
}

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

function Legend({ kind, threshold }: { kind: HeatKind; threshold: number }) {
  const bar = (background: string) => (
    <span style={{ width: 120, height: 10, borderRadius: 3, display: "inline-block", background }} />
  );
  let scale: ReactNode;
  if (kind === "bias") {
    scale = (
      <>
        Bajo el perfil
        {bar("linear-gradient(90deg in oklab, var(--div-neg), var(--div-mid), var(--div-pos))")}
        Sobre el perfil (satura en ±{Math.round((Math.exp(SCALE.bias.max) - 1) * 100)} %)
      </>
    );
  } else {
    scale = (
      <>
        0{bar("linear-gradient(90deg in oklab, var(--seq-lo), var(--seq-hi))")}
        {fmtNumber(SCALE[kind].max, 2)}+
      </>
    );
  }
  return (
    <div className="legend" style={{ marginBottom: 8 }}>
      <span className="legend-item">{scale}</span>
      <span className="legend-item">
        <svg width="10" height="10" aria-hidden="true">
          <circle cx="5" cy="5" r="3" fill="var(--ink)" />
        </svg>
        En alerta ({kind === "bias" ? `|log| > ${fmtNumber(threshold, 2)}` : `> ${fmtNumber(threshold, 2)}`})
      </span>
    </div>
  );
}

function TipRows({ kind, cell }: { kind: HeatKind; cell: HeatCell }) {
  const rows: [string, string][] =
    kind === "bias"
      ? [
          ["Real vs. perfil", fmtBias(cell.value)],
          ["log(real / perfil)", fmtSigned(cell.value, 3)],
        ]
      : kind === "psi"
        ? [
            ["PSI", fmtNumber(cell.value, 3)],
            ["Lectura", cell.value > 0.25 ? "cambio mayor" : cell.value > 0.1 ? "cambio moderado" : "estable"],
            ...(cell.extra != null ? ([["p-valor KS", cell.extra < 0.001 ? "< 0,001" : fmtNumber(cell.extra, 3)]] as [string, string][]) : []),
          ]
        : [
            ["Distancia de forma", fmtNumber(cell.value, 3)],
            ["Horas redistribuidas", `${fmtNumber(cell.value * 100, 1)} % de la demanda`],
          ];
  return (
    <>
      {rows.map(([label, value]) => (
        <div className="tooltip-row" key={label}>
          <span>{label}</span>
          <b>{value}</b>
        </div>
      ))}
    </>
  );
}

export function SignalHeatmap({
  kind,
  rows,
  cells,
  threshold,
  label,
}: {
  kind: HeatKind;
  rows: HeatRow[];
  cells: HeatCell[];
  threshold: number;
  label: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<{ cell: HeatCell; x: number; y: number } | null>(null);

  const times = [...new Set(cells.map((c) => c.ts))].sort((a, b) => a - b);
  const byKey = new Map(cells.map((c) => [`${c.station_id}|${c.ts}`, c]));
  const labelW = 136;
  const rowH = 22;
  const top = 4;
  const axis = 22;
  const plotW = Math.max(0, width - labelW);
  const cellW = times.length ? plotW / times.length : 0;
  const height = top + rows.length * rowH + axis;
  const names = new Map(rows.map((r) => [r.station_id, r.name]));

  // Una marca cada ~70 px.
  const every = Math.max(1, Math.ceil(70 / Math.max(cellW, 1)));
  const tickIdx = times.map((_, i) => i).filter((i) => i % every === 0);

  return (
    <div ref={ref} style={{ position: "relative" }} onMouseLeave={() => setHover(null)}>
      <Legend kind={kind} threshold={threshold} />
      {width > 0 && (
        <svg width={width} height={height} role="img" aria-label={label}>
          {rows.map((row, r) => (
            <g key={row.station_id}>
              <text x={labelW - 8} y={top + r * rowH + rowH / 2} dy={4} textAnchor="end" fontSize={11} fill="var(--ink-2)">
                {row.name}
              </text>
              {times.map((t, i) => {
                const cell = byKey.get(`${row.station_id}|${t}`);
                const x = labelW + i * cellW;
                const y = top + r * rowH;
                if (!cell) return <rect key={t} x={x + 1} y={y + 1} width={Math.max(0, cellW - 2)} height={rowH - 2} rx={2} fill="var(--surface-2)" />;
                return (
                  <g key={t} onMouseMove={(event) => setHover({ cell, x: event.clientX, y: event.clientY })}>
                    <rect x={x + 1} y={y + 1} width={Math.max(0, cellW - 2)} height={rowH - 2} rx={2} style={{ fill: cellColor(kind, cell.value) }} />
                    {cell.alert && cellW >= 6 && (
                      <circle cx={x + cellW / 2} cy={y + rowH / 2} r={Math.min(3, cellW / 4)} fill={kind === "bias" ? "var(--ink)" : "var(--surface)"} opacity={0.85} />
                    )}
                  </g>
                );
              })}
            </g>
          ))}
          {tickIdx.map((i) => {
            const x = labelW + i * cellW + cellW / 2;
            // La última marca se alinea a la derecha para no salirse del lienzo.
            const anchor = x + 20 > width ? "end" : "middle";
            return (
              <text key={i} x={anchor === "end" ? width : x} y={height - 6} textAnchor={anchor} fontSize={11} fill="var(--muted)">
                {fmtTick(times[i])}
              </text>
            );
          })}
        </svg>
      )}
      {hover && (
        <div className="tooltip floating-tip" style={{ left: hover.x, top: hover.y }}>
          <div className="tooltip-title">{names.get(hover.cell.station_id)}</div>
          <div className="tooltip-row">
            <span>Corte</span>
            <b>{fmtDateTime(hover.cell.ts)}</b>
          </div>
          <TipRows kind={kind} cell={hover.cell} />
          <div className="tooltip-row">
            <span>Estado</span>
            <b>{hover.cell.alert ? "⚠ En alerta" : "Normal"}</b>
          </div>
        </div>
      )}
    </div>
  );
}
