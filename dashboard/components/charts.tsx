"use client";

import type { ReactNode } from "react";
import {
  Area,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  LabelList,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { fmtBias, fmtDateTime, fmtNumber, fmtSigned, fmtTick, HORIZON_LABEL } from "@/lib/format";

// ---------------------------------------------------------------------------
// Piezas comunes: ejes recesivos, grid horizontal, tooltip propio.
// ---------------------------------------------------------------------------

const AXIS_TICK = { fill: "var(--muted)", fontSize: 11 };
const AXIS_LINE = { stroke: "var(--axis)" };

type TipRow = { label: ReactNode; value: ReactNode; color?: string; dash?: boolean };

function TipBox({ title, rows }: { title: ReactNode; rows: TipRow[] }) {
  return (
    <div className="tooltip">
      <div className="tooltip-title">{title}</div>
      {rows.map((row, index) => (
        <div className="tooltip-row" key={index}>
          <span className="legend-item">
            {row.color && <span className={row.dash ? "swatch-dash" : "swatch"} style={row.dash ? { borderColor: row.color } : { background: row.color }} />}
            {row.label}
          </span>
          <b>{row.value}</b>
        </div>
      ))}
    </div>
  );
}

type Payload = { payload: Record<string, unknown> }[];
type TipArgs = { active?: boolean; payload?: readonly unknown[] };

function tipPayload(args: TipArgs): Record<string, unknown> | null {
  if (!args.active || !args.payload?.length) return null;
  return (args.payload as Payload)[0].payload;
}

/** Marcas cada `hours` horas alineadas al reloj, para ejes de tiempo numéricos. */
function timeTicks(values: number[], hours: number): number[] {
  if (!values.length) return [];
  const step = hours * 3600_000;
  const min = Math.min(...values);
  const max = Math.max(...values);
  // Alinea a la hora de Bogotá (UTC−5).
  const offset = 5 * 3600_000;
  const first = Math.ceil((min - offset) / step) * step + offset;
  const ticks = [];
  for (let t = first; t <= max; t += step) ticks.push(t);
  return ticks;
}

/** Dominio y marcas redondas (paso 1, 2, 2,5, 5, 10, 20, 25…) para un eje numérico. */
function niceScale(lo: number, hi: number, target = 5): { domain: [number, number]; ticks: number[] } {
  const span = Math.max(hi - lo, 1e-9);
  const raw = span / target;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((m) => m >= raw) ?? 10 * power;
  const start = Math.floor(lo / step) * step;
  const end = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let t = start; t <= end + step / 2; t += step) ticks.push(Number(t.toFixed(6)));
  return { domain: [start, end], ticks };
}

function tickHours(values: number[]): number {
  if (values.length < 2) return 6;
  const spanHours = (Math.max(...values) - Math.min(...values)) / 3600_000;
  return spanHours > 96 ? 24 : spanHours > 48 ? 12 : spanHours > 18 ? 6 : 3;
}

export function Legend({ items }: { items: { label: string; color: string; kind?: "line" | "dash" | "box" | "dot" }[] }) {
  return (
    <div className="legend">
      {items.map((item) => (
        <span className="legend-item" key={item.label}>
          {item.kind === "dash" ? (
            <span className="swatch-dash" style={{ borderColor: item.color }} />
          ) : item.kind === "line" ? (
            <span className="swatch-line" style={{ background: item.color }} />
          ) : item.kind === "dot" ? (
            <span className="swatch" style={{ background: item.color, borderRadius: "50%" }} />
          ) : (
            <span className="swatch" style={{ background: item.color }} />
          )}
          {item.label}
        </span>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Resumen: accuracy oficial rolling y acumulada
// ---------------------------------------------------------------------------

export type AccuracyPoint = { ts: number; rolling: number | null; cumulative: number | null; rankRolling: number | null; rankCumulative: number | null };

export function AccuracyTrend({ data, alert }: { data: AccuracyPoint[]; alert: number }) {
  const ts = data.map((d) => d.ts);
  const values = data.flatMap((d) => [d.rolling, d.cumulative]).filter((v): v is number => v != null);
  const y = niceScale(Math.min(alert, ...values), Math.max(...values));
  return (
    <>
      <Legend
        items={[
          { label: "Rolling 24 h", color: "var(--series-1)", kind: "line" },
          { label: "Acumulada", color: "var(--series-2)", kind: "line" },
          { label: `Umbral de alerta (${alert})`, color: "var(--muted)", kind: "dash" },
        ]}
      />
      <div style={{ height: 260, marginTop: 8 }}>
        <ResponsiveContainer>
          <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: -8 }}>
            <CartesianGrid vertical={false} stroke="var(--grid)" />
            <XAxis dataKey="ts" type="number" scale="time" domain={["dataMin", "dataMax"]} ticks={timeTicks(ts, tickHours(ts))} tickFormatter={fmtTick} tick={AXIS_TICK} axisLine={AXIS_LINE} tickLine={false} />
            <YAxis domain={y.domain} ticks={y.ticks} tick={AXIS_TICK} axisLine={false} tickLine={false} width={40} tickFormatter={(v) => fmtNumber(v, 0)} />
            <ReferenceLine y={alert} stroke="var(--muted)" strokeDasharray="4 4" />
            <Tooltip
              cursor={{ stroke: "var(--axis)" }}
              content={(args) => {
                const p = tipPayload(args) as AccuracyPoint | null;
                if (!p) return null;
                return (
                  <TipBox
                    title={`Corte ${fmtDateTime(p.ts)}`}
                    rows={[
                      { label: "Rolling 24 h", value: `${fmtNumber(p.rolling, 2)} · #${p.rankRolling ?? "—"}`, color: "var(--series-1)" },
                      { label: "Acumulada", value: `${fmtNumber(p.cumulative, 2)} · #${p.rankCumulative ?? "—"}`, color: "var(--series-2)" },
                    ]}
                  />
                );
              }}
            />
            <Line dataKey="rolling" stroke="var(--series-1)" strokeWidth={2} dot={false} activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--surface)" }} connectNulls isAnimationActive={false} />
            <Line dataKey="cumulative" stroke="var(--series-2)" strokeWidth={2} dot={false} activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--surface)" }} connectNulls isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Drift: WAPE rolling (como accuracy) frente al umbral
// ---------------------------------------------------------------------------

export type WapePoint = { ts: number; accuracy: number; alert: boolean; n: number; h1?: number; h4?: number };

export function WapeTrend({ data, threshold }: { data: WapePoint[]; threshold: number }) {
  const ts = data.map((d) => d.ts);
  const values = data.map((d) => d.accuracy);
  const y = niceScale(Math.min(threshold, ...values) - 0.5, Math.max(...values) + 0.5);
  return (
    <>
      <Legend
        items={[
          { label: "Accuracy rolling 24 h (1 − WAPE)", color: "var(--series-1)", kind: "line" },
          { label: `Umbral ${fmtNumber(threshold, 0)}`, color: "var(--muted)", kind: "dash" },
          { label: "Corte en alerta", color: "var(--critical)", kind: "dot" },
        ]}
      />
      <div style={{ height: 240, marginTop: 8 }}>
        <ResponsiveContainer>
          <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: -8 }}>
            <CartesianGrid vertical={false} stroke="var(--grid)" />
            <XAxis dataKey="ts" type="number" scale="time" domain={["dataMin", "dataMax"]} ticks={timeTicks(ts, tickHours(ts))} tickFormatter={fmtTick} tick={AXIS_TICK} axisLine={AXIS_LINE} tickLine={false} />
            <YAxis domain={y.domain} ticks={y.ticks} tick={AXIS_TICK} axisLine={false} tickLine={false} width={40} tickFormatter={(v) => fmtNumber(v, 0)} />
            <ReferenceLine y={threshold} stroke="var(--muted)" strokeDasharray="4 4" />
            <Tooltip
              cursor={{ stroke: "var(--axis)" }}
              content={(args) => {
                const p = tipPayload(args) as WapePoint | null;
                if (!p) return null;
                return (
                  <TipBox
                    title={`Ventana que cierra ${fmtDateTime(p.ts)}`}
                    rows={[
                      { label: "Accuracy", value: fmtNumber(p.accuracy, 2), color: "var(--series-1)" },
                      { label: "WAPE", value: fmtNumber(1 - p.accuracy / 100, 3) },
                      ...(p.h1 != null ? [{ label: "A 15 min", value: fmtNumber(p.h1, 2) }] : []),
                      ...(p.h4 != null ? [{ label: "A 60 min", value: fmtNumber(p.h4, 2) }] : []),
                      { label: "Predicciones", value: p.n.toLocaleString("es-CO") },
                      { label: "Estado", value: p.alert ? "⚠ Alerta" : "Normal" },
                    ]}
                  />
                );
              }}
            />
            <Line
              dataKey="accuracy"
              stroke="var(--series-1)"
              strokeWidth={2}
              isAnimationActive={false}
              dot={(props) => {
                const { cx, cy, payload, index } = props as { cx: number; cy: number; payload: WapePoint; index: number };
                if (!payload.alert) return <g key={index} />;
                return <circle key={index} cx={cx} cy={cy} r={4} fill="var(--critical)" stroke="var(--surface)" strokeWidth={2} />;
              }}
              activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--surface)" }}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Drift: estaciones con sesgo en alerta por corte
// ---------------------------------------------------------------------------

export type AlertCountPoint = { ts: number; bias: number; psi: number | null; stations: string[]; psiStations: string[]; decision?: string };

export function AlertCountBars({ data, trigger }: { data: AlertCountPoint[]; trigger: number }) {
  const ts = data.map((d) => d.ts);
  const max = Math.max(trigger + 1, ...data.map((d) => Math.max(d.bias, d.psi ?? 0)));
  return (
    <>
      <Legend
        items={[
          { label: "Sesgo de nivel (concept)", color: "var(--series-1)" },
          { label: "PSI de la demanda (data)", color: "var(--series-2)" },
          { label: `Disparo de drift (≥ ${trigger})`, color: "var(--muted)", kind: "dash" },
        ]}
      />
      <div style={{ height: 220, marginTop: 8 }}>
        <ResponsiveContainer>
          <BarChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }} barCategoryGap={1} barGap={1}>
            <CartesianGrid vertical={false} stroke="var(--grid)" />
            <XAxis dataKey="ts" type="number" scale="time" domain={["dataMin - 1800000", "dataMax + 1800000"]} ticks={timeTicks(ts, tickHours(ts))} tickFormatter={fmtTick} tick={AXIS_TICK} axisLine={AXIS_LINE} tickLine={false} />
            <YAxis domain={[0, max]} allowDecimals={false} tick={AXIS_TICK} axisLine={false} tickLine={false} width={32} />
            <ReferenceLine y={trigger} stroke="var(--muted)" strokeDasharray="4 4" />
            <Tooltip
              cursor={{ fill: "var(--surface-2)" }}
              content={(args) => {
                const p = tipPayload(args) as AlertCountPoint | null;
                if (!p) return null;
                return (
                  <TipBox
                    title={`Corte ${fmtDateTime(p.ts)}`}
                    rows={[
                      { label: "Sesgo en alerta", value: `${p.bias} de 12`, color: "var(--series-1)" },
                      ...(p.stations.length ? [{ label: "", value: p.stations.join(", ") }] : []),
                      { label: "PSI en alerta", value: p.psi == null ? "sin dato" : `${p.psi} de 12`, color: "var(--series-2)" },
                      ...(p.psiStations.length ? [{ label: "", value: p.psiStations.join(", ") }] : []),
                      ...(p.decision ? [{ label: "Reentrenamiento", value: p.decision }] : []),
                    ]}
                  />
                );
              }}
            />
            <Bar dataKey="bias" fill="var(--series-1)" radius={[3, 3, 0, 0]} maxBarSize={8} isAnimationActive={false} />
            <Bar dataKey="psi" fill="var(--series-2)" radius={[3, 3, 0, 0]} maxBarSize={8} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Drift: sesgo actual por estación (divergente)
// ---------------------------------------------------------------------------

export type BiasBar = { station_id: string; name: string; bias: number; alert: boolean };

export function BiasBars({ data, threshold }: { data: BiasBar[]; threshold: number }) {
  const rows = [...data].sort((a, b) => b.bias - a.bias).map((d) => ({ ...d, pct: (Math.exp(d.bias) - 1) * 100 }));
  const upper = (Math.exp(threshold) - 1) * 100;
  const lower = (Math.exp(-threshold) - 1) * 100;
  const extent = Math.ceil(Math.max(Math.abs(lower), upper, ...rows.map((r) => Math.abs(r.pct))) / 5) * 5 + 5;
  return (
    <>
      <Legend
        items={[
          { label: "Demanda sobre el perfil", color: "var(--div-pos)" },
          { label: "Demanda bajo el perfil", color: "var(--div-neg)" },
          { label: `Umbral ±${fmtNumber(threshold, 2)} en log`, color: "var(--muted)", kind: "dash" },
        ]}
      />
      <div style={{ height: rows.length * 26 + 30, marginTop: 8 }}>
        <ResponsiveContainer>
          <BarChart data={rows} layout="vertical" margin={{ top: 0, right: 56, bottom: 0, left: 0 }} barCategoryGap={4}>
            <CartesianGrid horizontal={false} stroke="var(--grid)" />
            <XAxis type="number" domain={[-extent, extent]} tick={AXIS_TICK} axisLine={false} tickLine={false} tickFormatter={(v) => `${fmtSigned(v, 0)} %`} />
            <YAxis type="category" dataKey="name" tick={{ ...AXIS_TICK, fill: "var(--ink-2)" }} axisLine={AXIS_LINE} tickLine={false} width={130} />
            <ReferenceLine x={upper} stroke="var(--muted)" strokeDasharray="4 4" />
            <ReferenceLine x={lower} stroke="var(--muted)" strokeDasharray="4 4" />
            <Tooltip
              cursor={{ fill: "var(--surface-2)" }}
              content={(args) => {
                const p = tipPayload(args) as (BiasBar & { pct: number }) | null;
                if (!p) return null;
                return (
                  <TipBox
                    title={`${p.name} (${p.station_id})`}
                    rows={[
                      { label: "Real vs. perfil", value: fmtBias(p.bias) },
                      { label: "log(real / perfil)", value: fmtSigned(p.bias, 3) },
                      { label: "Estado", value: p.alert ? "⚠ En alerta" : "Normal" },
                    ]}
                  />
                );
              }}
            />
            <Bar dataKey="pct" radius={4} maxBarSize={16} isAnimationActive={false}>
              {rows.map((row) => (
                <Cell key={row.station_id} fill={row.pct >= 0 ? "var(--div-pos)" : "var(--div-neg)"} fillOpacity={row.alert ? 1 : 0.45} />
              ))}
              <LabelList
                dataKey="pct"
                content={(props) => {
                  const { x, y, width, height, index } = props as { x: number; y: number; width: number; height: number; index: number };
                  const row = rows[index];
                  if (!row?.alert) return null;
                  const end = row.pct >= 0 ? x + width + 6 : x + width - 6;
                  return (
                    <text x={end} y={y + height / 2} dy={4} textAnchor={row.pct >= 0 ? "start" : "end"} fontSize={11} fontWeight={600} fill="var(--ink)">
                      ⚠ {fmtBias(row.bias)}
                    </text>
                  );
                }}
              />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Desempeño: real vs. predicción enviada para una estación
// ---------------------------------------------------------------------------

export type SeriesRow = { ts: number; actual?: number; predicted?: number; band?: [number, number] };

export function StationSeriesChart({ data, horizonLabel }: { data: SeriesRow[]; horizonLabel: string }) {
  const ts = data.map((d) => d.ts);
  return (
    <>
      <Legend
        items={[
          { label: "Demanda real", color: "var(--ink-2)", kind: "line" },
          { label: `Predicción enviada (${horizonLabel})`, color: "var(--series-1)", kind: "line" },
          { label: "Intervalo enviado", color: "var(--band)" },
        ]}
      />
      <div style={{ height: 300, marginTop: 8 }}>
        <ResponsiveContainer>
          <ComposedChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: -4 }}>
            <CartesianGrid vertical={false} stroke="var(--grid)" />
            <XAxis dataKey="ts" type="number" scale="time" domain={["dataMin", "dataMax"]} ticks={timeTicks(ts, tickHours(ts))} tickFormatter={fmtTick} tick={AXIS_TICK} axisLine={AXIS_LINE} tickLine={false} />
            <YAxis tick={AXIS_TICK} axisLine={false} tickLine={false} width={52} tickFormatter={(v) => fmtNumber(v, 0)} />
            <Tooltip
              cursor={{ stroke: "var(--axis)" }}
              content={(args) => {
                const p = tipPayload(args) as SeriesRow | null;
                if (!p) return null;
                const error = p.actual != null && p.predicted != null && p.actual > 0 ? ((p.predicted - p.actual) / p.actual) * 100 : null;
                return (
                  <TipBox
                    title={fmtDateTime(p.ts)}
                    rows={[
                      { label: "Real", value: fmtNumber(p.actual, 0), color: "var(--ink-2)" },
                      { label: "Predicción", value: fmtNumber(p.predicted, 0), color: "var(--series-1)" },
                      ...(error != null ? [{ label: "Error", value: `${fmtSigned(error, 1)} %` }] : []),
                    ]}
                  />
                );
              }}
            />
            <Area dataKey="band" stroke="none" fill="var(--band)" connectNulls isAnimationActive={false} activeDot={false} />
            <Line dataKey="actual" stroke="var(--ink-2)" strokeWidth={1.5} dot={false} connectNulls isAnimationActive={false} activeDot={{ r: 3 }} />
            <Line dataKey="predicted" stroke="var(--series-1)" strokeWidth={2} dot={{ r: 2.5, strokeWidth: 0, fill: "var(--series-1)" }} connectNulls isAnimationActive={false} activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--surface)" }} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Desempeño: producción vs. validación (horizonte y estación)
// ---------------------------------------------------------------------------

export type CompareRow = { key: string; label: string; production: number | null; validation: number | null };

function CompareTip({ row }: { row: CompareRow }) {
  const gap = row.production != null && row.validation != null ? row.production - row.validation : null;
  return (
    <TipBox
      title={row.label}
      rows={[
        { label: "Producción (24 h)", value: fmtNumber(row.production, 2), color: "var(--series-1)" },
        { label: "Validación (backtest)", value: fmtNumber(row.validation, 2), color: "var(--series-3)" },
        ...(gap != null ? [{ label: "Diferencia", value: `${fmtSigned(gap, 2)} pts` }] : []),
      ]}
    />
  );
}

export function CompareBars({ data, layout = "horizontal", height = 240 }: { data: CompareRow[]; layout?: "horizontal" | "vertical"; height?: number }) {
  const values = data.flatMap((d) => [d.production, d.validation]).filter((v): v is number => v != null);
  const y = niceScale(Math.max(0, Math.min(...values) - 3), 100, 5);
  const vertical = layout === "vertical";
  return (
    <>
      <Legend
        items={[
          { label: "Producción (rolling 24 h)", color: "var(--series-1)" },
          { label: "Validación del campeón (backtest)", color: "var(--series-3)" },
        ]}
      />
      <div style={{ height, marginTop: 8 }}>
        <ResponsiveContainer>
          <BarChart data={data} layout={layout} margin={{ top: 4, right: 16, bottom: 0, left: vertical ? 0 : -12 }} barGap={2} barCategoryGap={vertical ? 6 : "24%"}>
            <CartesianGrid horizontal={!vertical} vertical={vertical} stroke="var(--grid)" />
            {vertical ? (
              <>
                <XAxis type="number" domain={y.domain} ticks={y.ticks} tick={AXIS_TICK} axisLine={false} tickLine={false} />
                <YAxis type="category" dataKey="label" tick={{ ...AXIS_TICK, fill: "var(--ink-2)" }} axisLine={AXIS_LINE} tickLine={false} width={130} />
              </>
            ) : (
              <>
                <XAxis dataKey="label" tick={{ ...AXIS_TICK, fill: "var(--ink-2)" }} axisLine={AXIS_LINE} tickLine={false} />
                <YAxis domain={y.domain} ticks={y.ticks} tick={AXIS_TICK} axisLine={false} tickLine={false} width={40} />
              </>
            )}
            <Tooltip cursor={{ fill: "var(--surface-2)" }} content={(args) => { const p = tipPayload(args) as CompareRow | null; return p ? <CompareTip row={p} /> : null; }} />
            <Bar dataKey="production" fill="var(--series-1)" radius={vertical ? [0, 4, 4, 0] : [4, 4, 0, 0]} maxBarSize={vertical ? 10 : 36} isAnimationActive={false}>
              <LabelList
                dataKey="production"
                position={vertical ? "right" : "top"}
                formatter={(v: unknown) => fmtNumber(v as number, 1)}
                style={{ fill: "var(--ink-2)", fontSize: 11 }}
              />
            </Bar>
            <Bar dataKey="validation" fill="var(--series-3)" radius={vertical ? [0, 4, 4, 0] : [4, 4, 0, 0]} maxBarSize={vertical ? 10 : 36} isAnimationActive={false}>
              <LabelList
                dataKey="validation"
                position={vertical ? "right" : "top"}
                formatter={(v: unknown) => fmtNumber(v as number, 1)}
                style={{ fill: "var(--muted)", fontSize: 10 }}
              />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Desempeño: distribución del error porcentual con signo
// ---------------------------------------------------------------------------

export type HistogramBin = { from: number; to: number; count: number; share: number; open?: "low" | "high" };

export function ErrorHistogram({ bins, mean, median }: { bins: HistogramBin[]; mean: number; median: number }) {
  const data = bins.map((b) => ({ ...b, mid: (b.from + b.to) / 2 }));
  const width = bins.length ? bins[0].to - bins[0].from : 5;
  return (
    <>
      <Legend
        items={[
          { label: "Predicciones por rango de error", color: "var(--series-1)" },
          { label: `Mediana ${fmtSigned(median, 1)} %`, color: "var(--ink-2)", kind: "dash" },
        ]}
      />
      <div style={{ height: 240, marginTop: 8 }}>
        <ResponsiveContainer>
          <BarChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }} barCategoryGap={1}>
            <CartesianGrid vertical={false} stroke="var(--grid)" />
            <XAxis
              dataKey="mid"
              type="number"
              domain={[data[0]?.from ?? -50, data[data.length - 1]?.to ?? 50]}
              ticks={data.filter((_, i) => i % 2 === 0).map((d) => d.from).concat(data.length ? [data[data.length - 1].to] : [])}
              tickFormatter={(v) => `${fmtSigned(v, 0)} %`}
              tick={AXIS_TICK}
              axisLine={AXIS_LINE}
              tickLine={false}
            />
            <YAxis tick={AXIS_TICK} axisLine={false} tickLine={false} width={40} allowDecimals={false} />
            <ReferenceLine x={median} stroke="var(--ink-2)" strokeDasharray="4 4" />
            <Tooltip
              cursor={{ fill: "var(--surface-2)" }}
              content={(args) => {
                const p = tipPayload(args) as (HistogramBin & { mid: number }) | null;
                if (!p) return null;
                const label = p.open === "low" ? `< ${fmtSigned(p.to, 0)} %` : p.open === "high" ? `> ${fmtSigned(p.from, 0)} %` : `${fmtSigned(p.from, 0)} % a ${fmtSigned(p.to, 0)} %`;
                return (
                  <TipBox
                    title={`Error ${label}`}
                    rows={[
                      { label: "Predicciones", value: p.count.toLocaleString("es-CO") },
                      { label: "Participación", value: `${fmtNumber(p.share * 100, 1)} %` },
                    ]}
                  />
                );
              }}
            />
            <Bar dataKey="count" fill="var(--series-1)" radius={[4, 4, 0, 0]} isAnimationActive={false} barSize={Math.max(4, width * 2)} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="card-foot">
        Error = (predicción − real) / real. Mediana {fmtSigned(median, 1)} %, media {fmtSigned(mean, 1)} %:{" "}
        {median < 0 ? "la predicción típica se queda corta" : "la predicción típica sobreestima"}, y la cola derecha (picos sobreestimados) empuja la media hacia arriba.
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Modelos: candidatos vs. campeón en cada decisión de reentrenamiento
// ---------------------------------------------------------------------------

export type DecisionPoint = { ts: number; champion: number | null; hl14?: number; hl5?: number; win7?: number; decision: string; trigger: string };

const RECIPE_COLORS: Record<string, string> = { hl14: "var(--series-1)", hl5: "var(--series-2)", win7: "var(--series-3)" };
const RECIPE_LABEL: Record<string, string> = { hl14: "hl14 · vida media 14 d", hl5: "hl5 · vida media 5 d", win7: "win7 · ventana 7 d" };

export function DecisionChart({ data }: { data: DecisionPoint[] }) {
  const ts = data.map((d) => d.ts);
  const values = data.flatMap((d) => [d.champion, d.hl14, d.hl5, d.win7]).filter((v): v is number => v != null);
  const y = niceScale(Math.min(...values) - 0.3, Math.max(...values) + 0.3);
  return (
    <>
      <Legend
        items={[
          ...Object.keys(RECIPE_COLORS).map((key) => ({ label: RECIPE_LABEL[key], color: RECIPE_COLORS[key], kind: "dot" as const })),
          { label: "Campeón en la misma ventana", color: "var(--ink)", kind: "line" as const },
        ]}
      />
      <div style={{ height: 260, marginTop: 8 }}>
        <ResponsiveContainer>
          <ComposedChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: -8 }}>
            <CartesianGrid vertical={false} stroke="var(--grid)" />
            <XAxis dataKey="ts" type="number" scale="time" domain={["dataMin - 3600000", "dataMax + 3600000"]} ticks={timeTicks(ts, tickHours(ts))} tickFormatter={fmtTick} tick={AXIS_TICK} axisLine={AXIS_LINE} tickLine={false} />
            <YAxis domain={y.domain} ticks={y.ticks} tick={AXIS_TICK} axisLine={false} tickLine={false} width={40} tickFormatter={(v) => fmtNumber(v, 0)} />
            <Tooltip
              cursor={{ stroke: "var(--axis)" }}
              content={(args) => {
                const p = tipPayload(args) as DecisionPoint | null;
                if (!p) return null;
                return (
                  <TipBox
                    title={`Corte ${fmtDateTime(p.ts)}`}
                    rows={[
                      { label: "Disparador", value: p.trigger },
                      { label: "Decisión", value: p.decision },
                      { label: "Campeón", value: fmtNumber(p.champion, 2), color: "var(--ink)" },
                      ...Object.keys(RECIPE_COLORS).map((key) => ({
                        label: key,
                        value: fmtNumber(p[key as "hl14"] ?? null, 2),
                        color: RECIPE_COLORS[key],
                      })),
                    ]}
                  />
                );
              }}
            />
            <Line dataKey="champion" stroke="var(--ink)" strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />
            {Object.keys(RECIPE_COLORS).map((key) => (
              <Scatter key={key} dataKey={key} fill={RECIPE_COLORS[key]} stroke="var(--surface)" strokeWidth={2} isAnimationActive={false} shape={(props: unknown) => {
                const { cx, cy } = props as { cx: number; cy: number };
                return cx == null || cy == null ? <g /> : <circle cx={cx} cy={cy} r={5} fill={RECIPE_COLORS[key]} stroke="var(--surface)" strokeWidth={2} />;
              }} />
            ))}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </>
  );
}

export { HORIZON_LABEL };
