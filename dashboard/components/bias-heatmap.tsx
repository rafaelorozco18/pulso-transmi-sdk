"use client";

import { useEffect, useRef, useState } from "react";

import { fmtBias, fmtDateTime, fmtSigned, fmtTick } from "@/lib/format";

export type HeatCell = {
  station_id: string;
  ts: number;
  bias: number;
  alert: boolean;
};
export type HeatRow = { station_id: string; name: string };

const CLIP = 0.3; // |log-ratio| a partir del cual el color satura (≈ ±35 %)

/** Divergente azul ↔ rojo con punto medio gris neutro; se mezcla en OKLab. */
function cellColor(bias: number): string {
  const share = Math.min(1, Math.abs(bias) / CLIP);
  const pole = bias >= 0 ? "var(--div-pos)" : "var(--div-neg)";
  return `color-mix(in oklab, ${pole} ${Math.round(share * 100)}%, var(--div-mid))`;
}

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver(([entry]) =>
      setWidth(entry.contentRect.width),
    );
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

export function BiasHeatmap({
  rows,
  cells,
  threshold,
}: {
  rows: HeatRow[];
  cells: HeatCell[];
  threshold: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<{
    cell: HeatCell;
    x: number;
    y: number;
  } | null>(null);

  const times = [...new Set(cells.map((c) => c.ts))].sort((a, b) => a - b);
  const byKey = new Map(cells.map((c) => [`${c.station_id}|${c.ts}`, c]));
  const label = 136;
  const rowH = 22;
  const top = 4;
  const axis = 22;
  const plotW = Math.max(0, width - label);
  const cellW = times.length ? plotW / times.length : 0;
  const height = top + rows.length * rowH + axis;
  const names = new Map(rows.map((r) => [r.station_id, r.name]));

  // Una marca cada ~70 px, preferentemente en medianoche o mediodía.
  const every = Math.max(1, Math.ceil(70 / Math.max(cellW, 1)));
  const tickIdx = times.map((_, i) => i).filter((i) => i % every === 0);

  return (
    <div
      ref={ref}
      style={{ position: "relative" }}
      onMouseLeave={() => setHover(null)}
    >
      <div className="legend" style={{ marginBottom: 8 }}>
        <span className="legend-item">
          Bajo el perfil
          <span
            style={{
              width: 120,
              height: 10,
              borderRadius: 3,
              display: "inline-block",
              background:
                "linear-gradient(90deg in oklab, var(--div-neg), var(--div-mid), var(--div-pos))",
            }}
          />
          Sobre el perfil (satura en ±{Math.round((Math.exp(CLIP) - 1) * 100)}{" "}
          %)
        </span>
        <span className="legend-item">
          <svg width="10" height="10" aria-hidden="true">
            <circle cx="5" cy="5" r="3" fill="var(--ink)" />
          </svg>
          En alerta (|log| &gt; {threshold})
        </span>
      </div>
      {width > 0 && (
        <svg
          width={width}
          height={height}
          role="img"
          aria-label="Mapa de calor del sesgo de nivel por estación y corte"
        >
          {rows.map((row, r) => (
            <g key={row.station_id}>
              <text
                x={label - 8}
                y={top + r * rowH + rowH / 2}
                dy={4}
                textAnchor="end"
                fontSize={11}
                fill="var(--ink-2)"
              >
                {row.name}
              </text>
              {times.map((t, i) => {
                const cell = byKey.get(`${row.station_id}|${t}`);
                const x = label + i * cellW;
                const y = top + r * rowH;
                if (!cell)
                  return (
                    <rect
                      key={t}
                      x={x + 1}
                      y={y + 1}
                      width={Math.max(0, cellW - 2)}
                      height={rowH - 2}
                      rx={2}
                      fill="var(--surface-2)"
                    />
                  );
                return (
                  <g
                    key={t}
                    onMouseMove={(event) =>
                      setHover({ cell, x: event.clientX, y: event.clientY })
                    }
                    style={{ cursor: "default" }}
                  >
                    <rect
                      x={x + 1}
                      y={y + 1}
                      width={Math.max(0, cellW - 2)}
                      height={rowH - 2}
                      rx={2}
                      style={{ fill: cellColor(cell.bias) }}
                    />
                    {cell.alert && cellW >= 6 && (
                      <circle
                        cx={x + cellW / 2}
                        cy={y + rowH / 2}
                        r={Math.min(3, cellW / 4)}
                        fill="var(--ink)"
                        opacity={0.75}
                      />
                    )}
                  </g>
                );
              })}
            </g>
          ))}
          {tickIdx.map((i) => {
            const x = label + i * cellW + cellW / 2;
            // La última marca se alinea a la derecha para no salirse del lienzo.
            const anchor = x + 20 > width ? "end" : "middle";
            return (
              <text
                key={i}
                x={anchor === "end" ? width : x}
                y={height - 6}
                textAnchor={anchor}
                fontSize={11}
                fill="var(--muted)"
              >
                {fmtTick(times[i])}
              </text>
            );
          })}
        </svg>
      )}
      {hover && (
        <div
          className="tooltip floating-tip"
          style={{ left: hover.x, top: hover.y }}
        >
          <div className="tooltip-title">
            {names.get(hover.cell.station_id)}
          </div>
          <div className="tooltip-row">
            <span>Corte</span>
            <b>{fmtDateTime(hover.cell.ts)}</b>
          </div>
          <div className="tooltip-row">
            <span>Real vs. perfil</span>
            <b>{fmtBias(hover.cell.bias)}</b>
          </div>
          <div className="tooltip-row">
            <span>log(real / perfil)</span>
            <b>{fmtSigned(hover.cell.bias, 3)}</b>
          </div>
          <div className="tooltip-row">
            <span>Estado</span>
            <b>{hover.cell.alert ? "⚠ En alerta" : "Normal"}</b>
          </div>
        </div>
      )}
    </div>
  );
}
