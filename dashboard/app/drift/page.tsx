import type { Metadata } from "next";

import { BiasHeatmap, type HeatCell } from "@/components/bias-heatmap";
import { AlertCountBars, BiasBars, WapeTrend, type AlertCountPoint, type WapePoint } from "@/components/charts";
import { StationMapClient } from "@/components/station-map-client";
import { Badge, Card, Empty, Tile } from "@/components/ui";
import { getDecisions, getDriftSignals, getSnapshots, getStations } from "@/lib/data";
import { fmtBias, fmtDateTime, fmtNumber, shortStation, TRIGGER_LABEL } from "@/lib/format";

export const metadata: Metadata = { title: "Drift · Pulso TransMi" };

// Réplica de pipeline/config.toml [performance]: cuántas estaciones con sesgo disparan drift.
const BIAS_ALERT_STATIONS = 2;

export default async function DriftPage() {
  const [signals, stations, decisions, snapshots] = await Promise.all([getDriftSignals(), getStations(), getDecisions(), getSnapshots()]);

  const names = new Map(stations.map((s) => [s.station_id, shortStation(s.station_name)]));
  const bias = signals.filter((s) => s.signal === "residual_bias" && s.station_id);
  const wape = signals.filter((s) => s.signal === "wape_rolling");
  const quality = signals.filter((s) => s.signal === "data_quality");

  if (!bias.length) {
    return (
      <div className="page-head">
        <h1>Drift</h1>
        <Empty>Aún no hay señales de drift calculadas.</Empty>
      </div>
    );
  }

  const threshold = bias[0].threshold ?? 0.1;
  const lastCut = bias.at(-1)!.window_end;
  const current = bias.filter((s) => s.window_end === lastCut);
  const currentAlerts = current.filter((s) => s.is_alert);
  const latestSnapshot = snapshots.at(-1);

  // Decisión de reentrenamiento tomada en cada corte (mismo data_cutoff).
  const decisionAt = new Map(decisions.filter((d) => d.cutoff).map((d) => [new Date(d.cutoff!).getTime(), d]));

  const cuts = [...new Set(bias.map((s) => s.window_end))];
  const alertCounts: AlertCountPoint[] = cuts.map((cut) => {
    const ts = new Date(cut).getTime();
    const inAlert = bias.filter((s) => s.window_end === cut && s.is_alert);
    const decision = decisionAt.get(ts);
    return {
      ts,
      count: inAlert.length,
      stations: inAlert.map((s) => names.get(s.station_id!) ?? s.station_id!),
      decision: decision ? `${TRIGGER_LABEL[decision.trigger] ?? decision.trigger} → ${decision.decision === "promote" ? "promovido" : "se conserva"}` : undefined,
    };
  });
  const driftCuts = alertCounts.filter((p) => p.count >= BIAS_ALERT_STATIONS).length;

  const cells: HeatCell[] = bias.map((s) => ({ station_id: s.station_id!, ts: new Date(s.window_end).getTime(), bias: s.value, alert: s.is_alert }));
  // Filas ordenadas por sesgo actual: las estaciones que más se alejan del perfil arriba.
  const currentBias = new Map(current.map((s) => [s.station_id!, s.value]));
  const heatRows = stations
    .map((s) => ({ station_id: s.station_id, name: shortStation(s.station_name) }))
    .sort((a, b) => (currentBias.get(b.station_id) ?? 0) - (currentBias.get(a.station_id) ?? 0));

  const wapeData: WapePoint[] = wape.map((s) => {
    const byHorizon = (s.details.by_horizon as Record<string, number>) ?? {};
    return {
      ts: new Date(s.window_end).getTime(),
      accuracy: (s.details.accuracy as number) ?? (1 - s.value) * 100,
      alert: s.is_alert,
      n: Number(s.details.n ?? 0),
      h1: byHorizon["1"],
      h4: byHorizon["4"],
    };
  });
  const wapeThreshold = wape.at(-1)?.threshold != null ? (1 - wape.at(-1)!.threshold!) * 100 : 82;
  const wapeNow = wapeData.at(-1);

  const qualityNow = quality.at(-1);
  const qualityAlerts = quality.filter((s) => s.is_alert).length;

  const mapStations = stations.map((s) => {
    const signal = current.find((c) => c.station_id === s.station_id);
    return {
      station_id: s.station_id,
      name: s.station_name,
      corridor: s.corridor,
      archetype: s.archetype,
      latitude: s.latitude,
      longitude: s.longitude,
      bias: signal?.value ?? null,
      alert: signal?.is_alert ?? false,
      accuracy: latestSnapshot?.by_station?.[s.station_id] ?? null,
    };
  });

  const alertLog = [...alertCounts].reverse().filter((p) => p.count > 0 || decisionAt.has(p.ts)).slice(0, 12);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Drift</h1>
          <p>
            Señales que la etapa de desempeño calcula en cada corte sobre una ventana rolling de 24 h virtuales y guarda en{" "}
            <code>pulso.drift_signals</code>. Una alerta de sesgo en {BIAS_ALERT_STATIONS} o más estaciones, o una accuracy bajo el umbral,
            dispara la evaluación de reentrenamiento.
          </p>
        </div>
        <span className="card-sub">Último corte evaluado: {fmtDateTime(lastCut)}</span>
      </div>

      <div className="grid grid-4">
        <Tile
          label="Estaciones con drift de nivel"
          value={currentAlerts.length}
          unit=" / 12"
          badge={currentAlerts.length >= BIAS_ALERT_STATIONS ? <Badge tone="warning">Drift activo</Badge> : <Badge tone="good">Estable</Badge>}
          meta={<>Umbral |log(real/perfil)| &gt; {fmtNumber(threshold, 2)}</>}
        />
        <Tile
          label="Cortes con drift disparado"
          value={driftCuts}
          unit={` / ${cuts.length}`}
          meta={<>Cortes con ≥ {BIAS_ALERT_STATIONS} estaciones en alerta</>}
        />
        <Tile
          label="Accuracy rolling (desempeño)"
          value={fmtNumber(wapeNow?.accuracy, 2)}
          badge={wapeNow ? wapeNow.alert ? <Badge tone="critical">Bajo umbral</Badge> : <Badge tone="good">Sobre umbral</Badge> : undefined}
          meta={<>Alerta si cae bajo {fmtNumber(wapeThreshold, 0)}</>}
        />
        <Tile
          label="Calidad de datos"
          value={qualityNow ? fmtNumber(qualityNow.value, 0) : "—"}
          unit=" filas faltantes"
          badge={qualityNow ? qualityNow.is_alert ? <Badge tone="critical">Huecos</Badge> : <Badge tone="good">Completa</Badge> : undefined}
          meta={<>{qualityAlerts} cortes con huecos en todo el historial</>}
        />
      </div>

      <Card
        className=""
        title="Sesgo de nivel por estación y corte"
        sub="Media de log(demanda real / perfil del campeón) en la ventana de 24 h. Rojo: la estación mueve más pasajeros de lo que el modelo espera; azul: menos."
      >
        <div style={{ marginTop: 16 }} />
        <BiasHeatmap rows={heatRows} cells={cells} threshold={threshold} />
      </Card>

      <div className="grid grid-2" style={{ marginTop: 16 }}>
        <Card title="Sesgo actual por estación" sub={`Corte ${fmtDateTime(lastCut)} · variación de la demanda frente al perfil`}>
          <BiasBars
            data={current.map((s) => ({ station_id: s.station_id!, name: names.get(s.station_id!) ?? s.station_id!, bias: s.value, alert: s.is_alert }))}
            threshold={threshold}
          />
        </Card>
        <Card title="Dónde está el drift" sub="Color = sesgo actual; borde grueso = estación en alerta. Pasa el cursor para ver el detalle.">
          <StationMapClient stations={mapStations} />
        </Card>
      </div>

      <div className="grid grid-2" style={{ marginTop: 16 }}>
        <Card title="Alertas de sesgo por corte" sub="Cuántas estaciones superan el umbral en cada corte evaluado">
          <AlertCountBars data={alertCounts} trigger={BIAS_ALERT_STATIONS} />
        </Card>
        <Card title="Drift de desempeño" sub="Accuracy oficial de las predicciones enviadas en la ventana rolling de 24 h">
          {wapeData.length ? <WapeTrend data={wapeData} threshold={wapeThreshold} /> : <Empty>Sin datos</Empty>}
        </Card>
      </div>

      <div className="grid grid-3" style={{ marginTop: 16 }}>
        <Card className="span-2" title="Bitácora de alertas y respuesta del pipeline" sub="Últimos cortes con alertas o con una decisión de reentrenamiento">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Corte (virtual)</th>
                  <th className="num">En alerta</th>
                  <th>Estaciones</th>
                  <th>Respuesta</th>
                </tr>
              </thead>
              <tbody>
                {alertLog.map((row) => (
                  <tr key={row.ts}>
                    <td className="tabular">{fmtDateTime(row.ts)}</td>
                    <td className="num">{row.count}</td>
                    <td>{row.stations.join(", ") || "—"}</td>
                    <td>
                      {row.decision ?? (row.count >= BIAS_ALERT_STATIONS ? <span className="card-sub">En cooldown (6 h)</span> : <span className="card-sub">Bajo el disparo</span>)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
        <Card title="Cómo se mide" sub="Configuración en pipeline/config.toml">
          <ul className="signal-list">
            <li>
              <b>Sesgo de nivel (concept drift)</b>
              <span>
                Media de log(real / perfil) por estación. Detecta cambios de nivel que el perfil entrenado ya no explica. Alerta si supera ±
                {fmtNumber(threshold, 2)} (≈ {fmtBias(threshold)}).
              </span>
            </li>
            <li>
              <b>WAPE rolling (desempeño)</b>
              <span>
                Métrica oficial sobre las predicciones realmente enviadas: WAPE por estación y promedio simple. Alerta si la accuracy cae bajo{" "}
                {fmtNumber(wapeThreshold, 0)} con al menos 96 evaluaciones.
              </span>
            </li>
            <li>
              <b>Calidad de datos</b>
              <span>Slots faltantes frente a los esperados (96 por estación en 24 h). Cualquier hueco es alerta.</span>
            </li>
            <li>
              <b>Respuesta</b>
              <span>
                Con drift se entrenan las recetas candidatas (hl14, hl5, win7) y se comparan con el campeón en ciclos simulados; solo se
                promueve si gana por 0,05 puntos.
              </span>
            </li>
          </ul>
        </Card>
      </div>
    </>
  );
}
