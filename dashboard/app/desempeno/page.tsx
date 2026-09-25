import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";

import { CompareBars, ErrorHistogram, StationSeriesChart, type CompareRow, type HistogramBin, type SeriesRow } from "@/components/charts";
import { StationPicker } from "@/components/station-picker";
import { Card, Empty, Tile } from "@/components/ui";
import { getModels, getRecentErrors, getSnapshots, getStationSeries, getStations } from "@/lib/data";
import { fmtNumber, fmtSigned, HORIZON_LABEL, shortStation } from "@/lib/format";

export const metadata: Metadata = { title: "Desempeño · Pulso TransMi" };

const HORIZONS = [1, 2, 3, 4];
const SERIES_HOURS = 72;

function histogram(values: number[], step = 5, limit = 50): HistogramBin[] {
  const bins: HistogramBin[] = [];
  for (let from = -limit; from < limit; from += step) bins.push({ from, to: from + step, count: 0, share: 0 });
  for (const value of values) {
    const index = Math.min(bins.length - 1, Math.max(0, Math.floor((value + limit) / step)));
    bins[index].count += 1;
  }
  for (const bin of bins) bin.share = values.length ? bin.count / values.length : 0;
  // Los extremos acumulan todo lo que cae fuera del rango.
  bins[0].open = "low";
  bins[bins.length - 1].open = "high";
  return bins;
}

function median(values: number[]): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

export default async function PerformancePage({ searchParams }: PageProps<"/desempeno">) {
  const params = await searchParams;
  const [stations, snapshots, models, errors] = await Promise.all([getStations(), getSnapshots(), getModels(), getRecentErrors()]);

  const latest = snapshots.at(-1);
  const champion = models.find((m) => m.status === "active");
  const validation = champion?.validation_metrics ?? {};

  // Estación por defecto: la de peor accuracy en producción (donde más interesa mirar).
  const worst = latest ? Object.entries(latest.by_station).sort((a, b) => a[1] - b[1])[0]?.[0] : undefined;
  const requested = typeof params.estacion === "string" ? params.estacion : undefined;
  const stationId = stations.some((s) => s.station_id === requested) ? requested! : worst ?? stations[0]?.station_id;
  const horizon = HORIZONS.includes(Number(params.h)) ? Number(params.h) : 1;
  const station = stations.find((s) => s.station_id === stationId);

  const raw = stationId ? await getStationSeries(stationId, horizon) : [];
  const end = raw.length ? new Date(raw.at(-1)!.t).getTime() : 0;
  const series: SeriesRow[] = raw
    .map((p) => ({
      ts: new Date(p.t).getTime(),
      actual: p.actual,
      predicted: p.predicted,
      band: p.lower != null && p.upper != null ? ([p.lower, p.upper] as [number, number]) : undefined,
    }))
    .filter((p) => p.ts > end - SERIES_HOURS * 3600_000);

  const stationErrors = series.filter((p) => p.actual != null && p.predicted != null && p.actual > 0);
  const stationWape = stationErrors.length
    ? stationErrors.reduce((acc, p) => acc + Math.abs(p.predicted! - p.actual!), 0) / stationErrors.reduce((acc, p) => acc + p.actual!, 0)
    : null;

  const horizonRows: CompareRow[] = HORIZONS.map((h) => ({
    key: String(h),
    label: HORIZON_LABEL(h),
    production: latest?.by_horizon?.[String(h)] ?? null,
    validation: validation.by_horizon?.[String(h)] ?? null,
  }));

  const stationRows: CompareRow[] = stations
    .map((s) => ({
      key: s.station_id,
      label: shortStation(s.station_name),
      production: latest?.by_station?.[s.station_id] ?? null,
      validation: validation.by_station?.[s.station_id] ?? null,
    }))
    .sort((a, b) => (a.production ?? 0) - (b.production ?? 0));

  const pct = errors.map((e) => e.pct_error * 100);
  const mean = pct.length ? pct.reduce((a, b) => a + b, 0) / pct.length : 0;
  const gap = latest?.accuracy != null && validation.accuracy != null ? latest.accuracy - validation.accuracy : null;
  const h1 = latest?.by_horizon?.["1"];
  const h4 = latest?.by_horizon?.["4"];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Desempeño</h1>
          <p>
            Predicciones realmente enviadas frente a la demanda que llegó después. Producción = ventana rolling de 24 h; validación = backtest
            del campeón en ciclos simulados antes de promoverlo. La brecha entre ambas es la huella del drift.
          </p>
        </div>
      </div>

      <div className="grid grid-4">
        <Tile label="Accuracy en producción (24 h)" value={fmtNumber(latest?.accuracy, 2)} meta={<>{latest?.n_predictions.toLocaleString("es-CO")} predicciones evaluadas</>} />
        <Tile label="Accuracy en validación" value={fmtNumber(validation.accuracy, 2)} meta={<>Backtest del campeón · receta {validation.recipe ?? "—"}</>} />
        <Tile
          label="Brecha producción − validación"
          value={gap != null ? fmtSigned(gap, 2) : "—"}
          unit=" pts"
          meta={<>{gap != null && gap < 0 ? "El modelo rinde menos que cuando se validó" : "Sin degradación"}</>}
        />
        <Tile
          label="Pérdida de 15 → 60 min"
          value={h1 != null && h4 != null ? fmtSigned(h4 - h1, 2) : "—"}
          unit=" pts"
          meta={<>La corrección de nivel se amortigua con el horizonte (0,8ʰ)</>}
        />
      </div>

      <Card
        title={`Real vs. predicción · ${station ? station.station_name : ""}`}
        sub={`Últimas ${SERIES_HOURS} h virtuales. WAPE de la estación en este tramo: ${stationWape != null ? fmtNumber(stationWape, 3) : "—"}`}
        action={
          <div className="controls">
            <Suspense>
              <StationPicker
                value={stationId ?? ""}
                stations={stations.map((s) => ({
                  id: s.station_id,
                  label: `${shortStation(s.station_name)} · ${fmtNumber(latest?.by_station?.[s.station_id], 1)}`,
                }))}
              />
            </Suspense>
            <nav className="segmented" aria-label="Horizonte">
              {HORIZONS.map((h) => (
                <Link key={h} href={`/desempeno?estacion=${stationId}&h=${h}`} aria-current={h === horizon ? "true" : undefined} scroll={false}>
                  {HORIZON_LABEL(h)}
                </Link>
              ))}
            </nav>
          </div>
        }
      >
        {series.length ? <StationSeriesChart data={series} horizonLabel={`a ${HORIZON_LABEL(horizon)}`} /> : <Empty>Sin datos para esta estación.</Empty>}
      </Card>

      <div className="grid grid-2" style={{ marginTop: 16 }}>
        <Card title="Accuracy por horizonte" sub="Cuánto se degrada el pronóstico al alejarse del corte">
          <CompareBars data={horizonRows} />
        </Card>
        <Card title="Distribución del error" sub={`${pct.length.toLocaleString("es-CO")} predicciones de las últimas 24 h, todas las estaciones y horizontes`}>
          {pct.length ? <ErrorHistogram bins={histogram(pct)} mean={mean} median={median(pct)} /> : <Empty>Sin predicciones evaluadas.</Empty>}
        </Card>
      </div>

      <Card
        className=""
        title="Accuracy por estación"
        sub="Ordenadas de peor a mejor en producción. Las estaciones con drift de nivel son las que más se alejan de su validación."
      >
        <div style={{ marginTop: 4 }} />
        <CompareBars data={stationRows} layout="vertical" height={stationRows.length * 34 + 30} />
      </Card>
    </>
  );
}
