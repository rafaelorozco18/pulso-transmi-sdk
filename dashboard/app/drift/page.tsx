import type { Metadata } from "next";

import { AlertCountBars, BiasBars, WapeTrend, type AlertCountPoint, type WapePoint } from "@/components/charts";
import { DistributionCompare, type StationDistribution } from "@/components/distribution-compare";
import { SignalHeatmap, type HeatCell } from "@/components/signal-heatmap";
import { StationMapClient } from "@/components/station-map-client";
import { Badge, Card, Empty, Tile } from "@/components/ui";
import { getDecisions, getDriftSignals, getSnapshots, getStations, type DriftSignal } from "@/lib/data";
import { fmtBias, fmtDateTime, fmtNumber, shortStation, TRIGGER_LABEL } from "@/lib/format";

export const metadata: Metadata = { title: "Drift · Pulso TransMi" };

// Réplica de pipeline/config.toml [performance]: estaciones en alerta que disparan drift.
const ALERT_STATIONS = 2;

function heatCells(signals: DriftSignal[]): HeatCell[] {
  return signals.map((s) => ({
    station_id: s.station_id!,
    ts: new Date(s.window_end).getTime(),
    value: s.value,
    alert: s.is_alert,
    extra: (s.details.ks_pvalue as number | undefined) ?? null,
  }));
}

function AlertCount({ count }: { count: number }) {
  return count >= ALERT_STATIONS ? <Badge tone="warning">Dispara drift</Badge> : count > 0 ? <Badge tone="neutral">Aislado</Badge> : <Badge tone="good">Estable</Badge>;
}

export default async function DriftPage() {
  const [signals, stations, decisions, snapshots] = await Promise.all([getDriftSignals(), getStations(), getDecisions(), getSnapshots()]);

  const names = new Map(stations.map((s) => [s.station_id, shortStation(s.station_name)]));
  const bySignal = (name: string) => signals.filter((s) => s.signal === name && s.station_id);
  const bias = bySignal("residual_bias");
  const psi = bySignal("demand_psi");
  const shape = bySignal("profile_shape");
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

  const lastCut = bias.at(-1)!.window_end;
  const at = (list: DriftSignal[], cut: string) => list.filter((s) => s.window_end === cut);
  const biasNow = at(bias, lastCut);
  const lastPsiCut = psi.at(-1)?.window_end;
  const psiNow = lastPsiCut ? at(psi, lastPsiCut) : [];
  const shapeNow = lastPsiCut ? at(shape, lastPsiCut) : [];
  const biasThreshold = bias[0].threshold ?? 0.1;
  const psiThreshold = psi[0]?.threshold ?? 0.25;
  const shapeThreshold = shape[0]?.threshold ?? 0.1;
  const latestSnapshot = snapshots.at(-1);

  const count = (list: DriftSignal[]) => list.filter((s) => s.is_alert).length;
  const biasAlerts = count(biasNow);
  const psiAlerts = count(psiNow);
  const shapeAlerts = count(shapeNow);

  // Decisión de reentrenamiento tomada en cada corte (mismo data_cutoff).
  const decisionAt = new Map(decisions.filter((d) => d.cutoff).map((d) => [new Date(d.cutoff!).getTime(), d]));
  const cuts = [...new Set(bias.map((s) => s.window_end))];
  const alertCounts: AlertCountPoint[] = cuts.map((cut) => {
    const ts = new Date(cut).getTime();
    const b = at(bias, cut).filter((s) => s.is_alert);
    const p = at(psi, cut);
    const decision = decisionAt.get(ts);
    return {
      ts,
      bias: b.length,
      psi: p.length ? p.filter((s) => s.is_alert).length : null,
      stations: b.map((s) => names.get(s.station_id!) ?? s.station_id!),
      psiStations: p.filter((s) => s.is_alert).map((s) => names.get(s.station_id!) ?? s.station_id!),
      decision: decision ? `${TRIGGER_LABEL[decision.trigger] ?? decision.trigger} → ${decision.decision === "promote" ? "promovido" : "se conserva"}` : undefined,
    };
  });
  const driftCuts = alertCounts.filter((p) => p.bias >= ALERT_STATIONS || (p.psi ?? 0) >= ALERT_STATIONS).length;

  // Filas ordenadas por sesgo actual: las estaciones que más se alejan arriba.
  const currentBias = new Map(biasNow.map((s) => [s.station_id!, s.value]));
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
    const signal = biasNow.find((c) => c.station_id === s.station_id);
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

  const distributions: StationDistribution[] = psiNow.map((s) => ({
    station_id: s.station_id!,
    name: names.get(s.station_id!) ?? s.station_id!,
    psi: s.value,
    ks_pvalue: (s.details.ks_pvalue as number) ?? null,
    median_ratio: (s.details.median_ratio as number) ?? null,
    alert: s.is_alert,
    reference: (s.details.reference_share as number[]) ?? [],
    current: (s.details.current_share as number[]) ?? [],
  }));

  const matrix = heatRows.map((row) => {
    const b = biasNow.find((s) => s.station_id === row.station_id);
    const p = psiNow.find((s) => s.station_id === row.station_id);
    const f = shapeNow.find((s) => s.station_id === row.station_id);
    const flags = [b, p, f].filter((s) => s?.is_alert).length;
    return { ...row, b, p, f, flags, accuracy: latestSnapshot?.by_station?.[row.station_id] ?? null };
  });

  const alertLog = [...alertCounts].reverse().filter((p) => p.bias > 0 || (p.psi ?? 0) > 0 || decisionAt.has(p.ts)).slice(0, 12);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Drift</h1>
          <p>
            La etapa de desempeño evalúa cada corte con una ventana rolling de 24 h virtuales y guarda las señales en{" "}
            <code>pulso.drift_signals</code>. Se vigilan tres frentes: <b>datos</b> (¿cambió la distribución de la demanda?), <b>concepto</b> (¿el
            perfil del modelo sigue explicando el nivel y la forma?) y <b>desempeño</b> (¿cayó la métrica oficial?). {ALERT_STATIONS} o más estaciones
            en alerta, o la accuracy bajo el umbral, disparan la evaluación de reentrenamiento.
          </p>
        </div>
        <span className="card-sub">Último corte evaluado: {fmtDateTime(lastCut)}</span>
      </div>

      <div className="grid grid-5">
        <Tile label="Data drift · PSI" value={psiAlerts} unit=" / 12" badge={<AlertCount count={psiAlerts} />} meta={<>PSI &gt; {fmtNumber(psiThreshold, 2)} (cambio mayor)</>} />
        <Tile label="Concepto · nivel" value={biasAlerts} unit=" / 12" badge={<AlertCount count={biasAlerts} />} meta={<>|log(real/perfil)| &gt; {fmtNumber(biasThreshold, 2)}</>} />
        <Tile label="Concepto · forma horaria" value={shapeAlerts} unit=" / 12" badge={<AlertCount count={shapeAlerts} />} meta={<>Distancia &gt; {fmtNumber(shapeThreshold, 2)}</>} />
        <Tile
          label="Desempeño · accuracy 24 h"
          value={fmtNumber(wapeNow?.accuracy, 2)}
          badge={wapeNow ? wapeNow.alert ? <Badge tone="critical">Bajo umbral</Badge> : <Badge tone="good">Sobre umbral</Badge> : undefined}
          meta={<>Alerta bajo {fmtNumber(wapeThreshold, 0)}</>}
        />
        <Tile
          label="Calidad de datos"
          value={qualityNow ? fmtNumber(qualityNow.value, 0) : "—"}
          unit=" faltantes"
          badge={qualityNow ? qualityNow.is_alert ? <Badge tone="critical">Huecos</Badge> : <Badge tone="good">Completa</Badge> : undefined}
          meta={<>{qualityAlerts} cortes con huecos · {driftCuts}/{cuts.length} cortes con drift</>}
        />
      </div>

      <Card
        title="Matriz de drift por estación"
        sub={`Último corte (${fmtDateTime(lastCut)}). Las estaciones con alertas en varias señales a la vez son las que perdieron más accuracy.`}
      >
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Estación</th>
                <th className="num">Nivel vs. perfil</th>
                <th className="num">PSI demanda</th>
                <th className="num">p-valor KS</th>
                <th className="num">Forma horaria</th>
                <th className="num">Accuracy 24 h</th>
                <th>Señales en alerta</th>
              </tr>
            </thead>
            <tbody>
              {matrix.map((row) => {
                const ks = row.p?.details.ks_pvalue as number | undefined;
                return (
                  <tr key={row.station_id}>
                    <td>
                      {row.name} <span className="card-sub">· {row.station_id}</span>
                    </td>
                    <td className={`num${row.b?.is_alert ? " cell-alert" : ""}`}>{row.b?.is_alert ? "⚠ " : ""}{fmtBias(row.b?.value)}</td>
                    <td className={`num${row.p?.is_alert ? " cell-alert" : ""}`}>{row.p?.is_alert ? "⚠ " : ""}{fmtNumber(row.p?.value, 3)}</td>
                    <td className="num">{ks == null ? "—" : ks < 0.001 ? "< 0,001" : fmtNumber(ks, 3)}</td>
                    <td className={`num${row.f?.is_alert ? " cell-alert" : ""}`}>{row.f?.is_alert ? "⚠ " : ""}{fmtNumber(row.f?.value, 3)}</td>
                    <td className="num">{fmtNumber(row.accuracy, 1)}</td>
                    <td>
                      {row.flags === 0 ? <Badge tone="good">Ninguna</Badge> : <Badge tone={row.flags >= 2 ? "critical" : "warning"}>{row.flags} de 3</Badge>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <h2 className="section-title">Concept drift · nivel frente al perfil</h2>
      <Card
        title="Sesgo de nivel por estación y corte"
        sub="Media de log(demanda real / perfil del campeón) en la ventana de 24 h. Rojo: la estación mueve más pasajeros de lo que el modelo espera; azul: menos."
      >
        <SignalHeatmap kind="bias" rows={heatRows} cells={heatCells(bias)} threshold={biasThreshold} label="Mapa de calor del sesgo de nivel por estación y corte" />
      </Card>
      <div className="grid grid-2" style={{ marginTop: 16 }}>
        <Card title="Sesgo actual por estación" sub={`Corte ${fmtDateTime(lastCut)} · variación de la demanda frente al perfil`}>
          <BiasBars
            data={biasNow.map((s) => ({ station_id: s.station_id!, name: names.get(s.station_id!) ?? s.station_id!, bias: s.value, alert: s.is_alert }))}
            threshold={biasThreshold}
          />
        </Card>
        <Card title="Dónde está el drift" sub="Color = sesgo actual; borde grueso = estación en alerta. Pasa el cursor para ver el detalle.">
          <StationMapClient stations={mapStations} />
        </Card>
      </div>

      <h2 className="section-title">Data drift · distribución de la demanda</h2>
      {psi.length ? (
        <>
          <Card
            title="PSI de la demanda por estación y corte"
            sub="Population Stability Index de log(demanda) en la ventana de 24 h frente a los últimos 14 días que vio el campeón, en las mismas horas y tipo de día. < 0,10 estable · 0,10–0,25 moderado · > 0,25 cambio mayor."
          >
            <SignalHeatmap kind="psi" rows={heatRows} cells={heatCells(psi)} threshold={psiThreshold} label="Mapa de calor del PSI de la demanda por estación y corte" />
          </Card>
          <div className="grid grid-2" style={{ marginTop: 16 }}>
            <Card title="Referencia vs. ventana actual" sub="Participación de la demanda actual en cada decil de la referencia (sin drift, ≈ 10 % en cada uno)">
              <DistributionCompare stations={distributions} />
            </Card>
            <Card title="Forma del perfil horario" sub="Fracción de la demanda del día que cayó en horas distintas a las que espera el perfil (distancia de variación total)">
              <SignalHeatmap kind="shape" rows={heatRows} cells={heatCells(shape)} threshold={shapeThreshold} label="Mapa de calor de la distancia de forma por estación y corte" />
            </Card>
          </div>
        </>
      ) : (
        <Card>
          <Empty>El data drift se empezará a medir en el próximo ciclo.</Empty>
        </Card>
      )}

      <h2 className="section-title">Drift de desempeño y respuesta del pipeline</h2>
      <div className="grid grid-2">
        <Card title="Estaciones en alerta por corte" sub="Concept drift (sesgo) y data drift (PSI) en cada corte evaluado">
          <AlertCountBars data={alertCounts} trigger={ALERT_STATIONS} />
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
                  <th className="num">Sesgo</th>
                  <th className="num">PSI</th>
                  <th>Estaciones</th>
                  <th>Respuesta</th>
                </tr>
              </thead>
              <tbody>
                {alertLog.map((row) => (
                  <tr key={row.ts}>
                    <td className="tabular" style={{ whiteSpace: "nowrap" }}>
                      {fmtDateTime(row.ts)}
                    </td>
                    <td className="num">{row.bias}</td>
                    <td className="num">{row.psi ?? "—"}</td>
                    <td>{[...new Set([...row.stations, ...row.psiStations])].join(", ") || "—"}</td>
                    <td>
                      {row.decision ??
                        (row.bias >= ALERT_STATIONS || (row.psi ?? 0) >= ALERT_STATIONS ? (
                          <span className="card-sub">En cooldown (6 h)</span>
                        ) : (
                          <span className="card-sub">Bajo el disparo</span>
                        ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
        <Card title="Cómo se mide" sub="Umbrales en pipeline/config.toml · código en pipeline/performance.py y drift.py">
          <ul className="signal-list">
            <li>
              <b>Data drift · PSI y KS</b>
              <span>
                Distribución de log(demanda) de la ventana frente a la referencia del campeón, en las mismas horas y tipo de día. Alerta con PSI &gt;{" "}
                {fmtNumber(psiThreshold, 2)}; el test KS de dos muestras lo confirma.
              </span>
            </li>
            <li>
              <b>Concepto · nivel</b>
              <span>
                Media de log(real / perfil). Alerta si supera ±{fmtNumber(biasThreshold, 2)} (≈ {fmtBias(biasThreshold)}).
              </span>
            </li>
            <li>
              <b>Concepto · forma</b>
              <span>Distancia entre la distribución horaria real y la del perfil. Ruido base ≈ 0,04; alerta sobre {fmtNumber(shapeThreshold, 2)}.</span>
            </li>
            <li>
              <b>Desempeño y datos</b>
              <span>
                Accuracy oficial de lo enviado (alerta bajo {fmtNumber(wapeThreshold, 0)}) y slots faltantes en la ventana.
              </span>
            </li>
            <li>
              <b>Respuesta</b>
              <span>
                Con drift (y 6 h de cooldown) se entrenan las recetas candidatas y se comparan con el campeón en ciclos simulados sin fuga; solo se
                promueve si gana por 0,05 puntos.
              </span>
            </li>
          </ul>
        </Card>
      </div>
    </>
  );
}
