import Link from "next/link";

import { AccuracyTrend, type AccuracyPoint } from "@/components/charts";
import { Badge, Card, Delta, Empty, RunStatusBadge, Tile } from "@/components/ui";
import { getClock, getCycles, getDecisions, getDriftSignals, getLeaderboard, getModels, getSnapshots, getStageStatus, getStations, type BoardRow } from "@/lib/data";
import { fmtAgo, fmtDateTime, fmtDuration, fmtNumber, shortStation, shortVersion, STAGE_LABEL, TRIGGER_LABEL } from "@/lib/format";

const ACCURACY_ALERT = 82;

function stageDetail(stage: string, details: Record<string, unknown>): string {
  const d = details as Record<string, string | number | null | undefined>;
  switch (stage) {
    case "collector":
      return `${d.rows ?? "—"} filas validadas · +${d.inserted ?? 0} nuevas`;
    case "inference":
      return d.status === "accepted" ? `48 predicciones aceptadas · intento ${d.attempt ?? 1}` : String(d.reason ?? d.status ?? "—");
    case "performance":
      return `accuracy ${d.accuracy ?? "—"} · cobertura ${d.coverage ?? "—"}`;
    case "retraining":
      return String(d.reason ?? (d.decision ? `decisión: ${d.decision}` : "—"));
    default:
      return "";
  }
}

const BOARD_TOP = 8;

function BoardTable({ rows }: { rows: BoardRow[] }) {
  const me = rows.find((r) => r.is_me);
  const shown = rows.slice(0, BOARD_TOP);
  if (me && !shown.includes(me)) shown.push(me);
  return (
    <table>
      <thead>
        <tr>
          <th className="num">#</th>
          <th>Participante</th>
          <th className="num">Accuracy</th>
          <th className="num">Cobertura</th>
        </tr>
      </thead>
      <tbody>
        {shown.map((row, index) => (
          <tr key={`${row.rank}-${index}`} className={row.is_me ? "me" : undefined}>
            <td className="num">{row.rank}</td>
            <td>{row.is_me ? `${row.display_name} (nosotros)` : row.kind === "student" ? "Otro participante" : `Referencia (${row.kind})`}</td>
            <td className="num">{fmtNumber(row.accuracy, 2)}</td>
            <td className="num">{fmtNumber(row.coverage * 100, 0)} %</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default async function OverviewPage() {
  const [snapshots, stages, cycles, models, decisions, signals, stations, clock, boardRolling, boardCumulative] = await Promise.all([
    getSnapshots(),
    getStageStatus(),
    getCycles(),
    getModels(),
    getDecisions(),
    getDriftSignals(),
    getStations(),
    getClock(),
    getLeaderboard("rolling_24h"),
    getLeaderboard("cumulative"),
  ]);

  const now = clock.now;
  const latest = snapshots.at(-1);
  const previous = snapshots.at(-2);
  const champion = models.find((m) => m.status === "active");
  const lastDecision = decisions.at(-1);
  const names = new Map(stations.map((s) => [s.station_id, shortStation(s.station_name)]));

  const accepted = cycles.filter((c) => c.accepted).length;
  const recentCycles = cycles.slice(-48);

  const lastCut = signals.filter((s) => s.signal === "residual_bias").at(-1)?.window_end;
  const biasNow = signals.filter((s) => s.signal === "residual_bias" && s.window_end === lastCut);
  const biasAlerts = biasNow.filter((s) => s.is_alert);
  const wapeNow = signals.filter((s) => s.signal === "wape_rolling").at(-1);
  const dqNow = signals.filter((s) => s.signal === "data_quality").at(-1);
  const lastPsiCut = signals.filter((s) => s.signal === "demand_psi").at(-1)?.window_end;
  const psiAlerts = signals.filter((s) => s.signal === "demand_psi" && s.window_end === lastPsiCut && s.is_alert);
  const driftStations = [...new Set([...biasAlerts, ...psiAlerts].map((s) => s.station_id!))];

  const trend: AccuracyPoint[] = snapshots.map((s) => ({
    ts: new Date(s.data_cutoff).getTime(),
    rolling: s.lb_rolling?.accuracy ?? s.accuracy,
    cumulative: s.lb_cumulative?.accuracy ?? null,
    rankRolling: s.lb_rolling?.rank ?? null,
    rankCumulative: s.lb_cumulative?.rank ?? null,
  }));

  const rolling = latest?.lb_rolling?.accuracy ?? latest?.accuracy ?? null;
  const rollingPrev = previous?.lb_rolling?.accuracy ?? previous?.accuracy ?? null;
  const cumulative = latest?.lb_cumulative?.accuracy ?? null;
  const cumulativePrev = previous?.lb_cumulative?.accuracy ?? null;
  const failing = stages.filter((s) => s.status === "failed");

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Resumen</h1>
          <p>
            Estado del pronóstico en producción: accuracy oficial del leaderboard, cobertura de ciclos, drift actual y la última ejecución
            de cada etapa del pipeline.
          </p>
        </div>
        {failing.length ? (
          <Badge tone="critical">{failing.length} etapa(s) con falla</Badge>
        ) : (
          <Badge tone="good">Pipeline operando</Badge>
        )}
      </div>

      <div className="grid grid-4">
        <Tile
          label="Accuracy rolling 24 h"
          value={fmtNumber(rolling, 2)}
          badge={latest?.lb_rolling?.rank ? <span className="badge">Puesto #{latest.lb_rolling.rank}</span> : undefined}
          meta={
            <>
              <Delta value={rolling != null && rollingPrev != null ? rolling - rollingPrev : null} /> vs. corte anterior
            </>
          }
        />
        <Tile
          label="Accuracy acumulada"
          value={fmtNumber(cumulative, 2)}
          badge={latest?.lb_cumulative?.rank ? <span className="badge">Puesto #{latest.lb_cumulative.rank}</span> : undefined}
          meta={
            <>
              <Delta value={cumulative != null && cumulativePrev != null ? cumulative - cumulativePrev : null} /> vs. corte anterior
            </>
          }
        />
        <Tile
          label="Cobertura de ciclos"
          value={`${accepted}/${cycles.length}`}
          badge={accepted === cycles.length ? <Badge tone="good">Completa</Badge> : <Badge tone="critical">{cycles.length - accepted} perdidos</Badge>}
          meta={<>Ciclo no enviado = error total en el leaderboard</>}
        />
        <Tile
          label="Estaciones con drift"
          value={driftStations.length}
          unit=" / 12"
          badge={driftStations.length >= 2 ? <Badge tone="warning">Drift activo</Badge> : <Badge tone="good">Estable</Badge>}
          meta={<>{driftStations.length ? driftStations.map((id) => names.get(id)).join(", ") : "Ninguna estación fuera de umbral"}</>}
        />
      </div>

      <div className="grid grid-3" style={{ marginTop: 16 }}>
        <Card
          className="span-2"
          title="Accuracy oficial en el tiempo"
          sub="Leaderboard de la competencia en cada corte evaluado (hora virtual). Accuracy = 100 × (1 − WAPE), promedio de las 12 estaciones."
          foot="Los valores bajos del inicio vienen de ciclos no enviados antes del vigilante continuo: el leaderboard cuenta cada ciclo perdido como error total."
        >
          {trend.length ? <AccuracyTrend data={trend} alert={ACCURACY_ALERT} /> : <Empty>Aún no hay cortes evaluados.</Empty>}
        </Card>

        <div className="stack">
          <Card title="Modelo campeón" sub={champion ? `Promovido ${fmtDateTime(champion.promoted_at)}` : undefined}>
            {champion ? (
              <div style={{ display: "grid", gap: 6 }}>
                <div style={{ fontSize: 18, fontWeight: 650 }}>{shortVersion(champion.model_version)}</div>
                <div className="card-sub mono" style={{ overflowWrap: "anywhere" }}>
                  {champion.model_version}
                </div>
                <div className="tile-meta" style={{ marginTop: 4 }}>
                  <span>Datos hasta {fmtDateTime(champion.training_data_end)}</span>
                  <span>· Validación {fmtNumber(champion.validation_metrics.accuracy, 2)}</span>
                </div>
              </div>
            ) : (
              <Empty>Sin modelo activo.</Empty>
            )}
          </Card>
          <Card title="Último reentrenamiento" sub={lastDecision ? `Evaluado ${fmtAgo(lastDecision.evaluated_at, now)}` : undefined}>
            {lastDecision ? (
              <div style={{ display: "grid", gap: 8 }}>
                <div className="controls">
                  <span className="badge">Disparador: {TRIGGER_LABEL[lastDecision.trigger] ?? lastDecision.trigger}</span>
                  {lastDecision.decision === "promote" ? <Badge tone="good">Promovido</Badge> : <Badge tone="neutral">Se conserva</Badge>}
                </div>
                <div className="card-sub" style={{ color: "var(--ink-2)" }}>
                  {lastDecision.reason}
                </div>
                <Link href="/modelos" className="card-sub">
                  Ver historial de decisiones →
                </Link>
              </div>
            ) : (
              <Empty>Sin decisiones registradas.</Empty>
            )}
          </Card>
        </div>
      </div>

      <Card
        title="Cobertura de los últimos ciclos"
        sub={`Cada barra es un ciclo oficial (4 horizontes × 12 estaciones). ${recentCycles.length} más recientes.`}
        className=""
        foot={
          <span className="legend">
            <span className="legend-item">
              <span className="swatch" style={{ background: "var(--series-1)" }} /> Submission aceptada
            </span>
            <span className="legend-item">
              <span className="swatch" style={{ background: "repeating-linear-gradient(45deg, var(--critical) 0 2px, transparent 2px 4px)", border: "1px solid var(--critical)" }} />{" "}
              Ciclo perdido
            </span>
          </span>
        }
      >
        <div style={{ marginTop: 4 }} />
        <div className="coverage-strip" role="list" aria-label="Cobertura de ciclos">
          {recentCycles.map((cycle) => (
            <div
              key={cycle.cycle_id}
              role="listitem"
              className={`coverage-cell${cycle.accepted ? "" : " missed"}`}
              title={`Corte ${fmtDateTime(cycle.data_cutoff)} · ${cycle.accepted ? `aceptado ${fmtDateTime(cycle.accepted_at)}` : "NO enviado"}`}
            />
          ))}
        </div>
        <div className="tile-meta" style={{ justifyContent: "space-between" }}>
          <span>{recentCycles[0] ? fmtDateTime(recentCycles[0].data_cutoff) : ""}</span>
          <span>{recentCycles.at(-1) ? fmtDateTime(recentCycles.at(-1)!.data_cutoff) : ""}</span>
        </div>
      </Card>

      <h2 className="section-title">Pipeline en GitHub Actions</h2>
      <Card
        sub={
          <>
            Vigilante continuo: sondea la API cada 45 s y en cada ciclo nuevo encadena las cuatro etapas. Última ingesta{" "}
            {fmtAgo(clock.last_ingestion_at, now)}.
          </>
        }
        title="Última ejecución por etapa"
      >
        <div className="pipeline">
          {stages.map((stage, index) => (
            <div className="stage" key={stage.stage}>
              <div className="stage-name">
                <span className="stage-step">{index + 1}</span>
                {STAGE_LABEL[stage.stage]}
              </div>
              <RunStatusBadge status={stage.status} />
              <div className="stage-detail">{stage.error_message ?? stageDetail(stage.stage, stage.details)}</div>
              <div className="card-sub">
                {fmtAgo(stage.started_at, now)} · {fmtDuration(stage.duration_s)}
              </div>
            </div>
          ))}
        </div>
      </Card>

      <h2 className="section-title">Señales de drift en el último corte</h2>
      <div className="grid grid-4">
        <Tile
          label="Datos · PSI de la demanda"
          value={lastPsiCut ? psiAlerts.length : "—"}
          unit={lastPsiCut ? " estaciones" : undefined}
          badge={lastPsiCut ? psiAlerts.length >= 2 ? <Badge tone="warning">Dispara drift</Badge> : <Badge tone="good">Normal</Badge> : undefined}
          meta={<>PSI &gt; 0,25 frente a la referencia del campeón</>}
        />
        <Tile
          label="Desempeño · WAPE rolling"
          value={wapeNow ? fmtNumber(wapeNow.value, 3) : "—"}
          badge={wapeNow ? wapeNow.is_alert ? <Badge tone="critical">Alerta</Badge> : <Badge tone="good">Normal</Badge> : undefined}
          meta={<>Umbral {wapeNow?.threshold != null ? fmtNumber(wapeNow.threshold, 2) : "—"} (accuracy &lt; {ACCURACY_ALERT})</>}
        />
        <Tile
          label="Concepto · sesgo de nivel"
          value={biasAlerts.length}
          unit=" estaciones"
          badge={biasAlerts.length >= 2 ? <Badge tone="warning">Dispara drift</Badge> : <Badge tone="good">Normal</Badge>}
          meta={<>|log(real/perfil)| &gt; 0,10; ≥ 2 estaciones disparan reentrenamiento</>}
        />
        <Tile
          label="Datos · calidad"
          value={dqNow ? fmtNumber(dqNow.value, 0) : "—"}
          unit=" filas faltantes"
          badge={dqNow ? dqNow.is_alert ? <Badge tone="critical">Huecos</Badge> : <Badge tone="good">Completo</Badge> : undefined}
          meta={<>Ventana de 24 h × 12 estaciones</>}
        />
      </div>
      <div style={{ marginTop: 12 }}>
        <Link href="/drift" className="card-sub">
          Ver el detalle del drift →
        </Link>
      </div>

      <h2 className="section-title">Leaderboard de la competencia</h2>
      <div className="grid grid-2">
        <Card title="Rolling 24 h" sub={`Top ${BOARD_TOP} y nuestra posición · actualizado ${fmtAgo(boardRolling.fetched_at, now)}`}>
          {boardRolling.rows.length ? <BoardTable rows={boardRolling.rows} /> : <Empty>Sin datos del leaderboard.</Empty>}
        </Card>
        <Card title="Acumulado" sub={`Top ${BOARD_TOP} y nuestra posición · actualizado ${fmtAgo(boardCumulative.fetched_at, now)}`}>
          {boardCumulative.rows.length ? <BoardTable rows={boardCumulative.rows} /> : <Empty>Sin datos del leaderboard.</Empty>}
        </Card>
      </div>
      <div className="card-foot">Los demás participantes se muestran de forma anónima: este dashboard es público.</div>
    </>
  );
}
