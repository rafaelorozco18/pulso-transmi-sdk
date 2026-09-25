import type { Metadata } from "next";

import { DecisionChart, type DecisionPoint } from "@/components/charts";
import { Badge, Card, Empty, RunStatusBadge, Tile } from "@/components/ui";
import { getClock, getDecisions, getModels, getRecentRuns } from "@/lib/data";
import { fmtAgo, fmtDateTime, fmtDuration, fmtNumber, shortVersion, STAGE_LABEL, TRIGGER_LABEL } from "@/lib/format";

export const metadata: Metadata = { title: "Modelos y pipeline · Pulso TransMi" };

const REPO_URL = "https://github.com/rafaelorozco18/pulso-transmi-sdk";

function StatusTag({ status }: { status: string }) {
  if (status === "active") return <Badge tone="good">Activo</Badge>;
  if (status === "retired") return <Badge tone="neutral">Retirado</Badge>;
  if (status === "rejected") return <Badge tone="critical">Rechazado</Badge>;
  return <Badge tone="neutral">Candidato</Badge>;
}

export default async function ModelsPage() {
  const [models, decisions, runs, clock] = await Promise.all([getModels(), getDecisions(), getRecentRuns(), getClock()]);
  const now = clock.now;
  const champion = models.find((m) => m.status === "active");

  const points: DecisionPoint[] = decisions
    .filter((d) => d.cutoff)
    .map((d) => ({
      ts: new Date(d.cutoff!).getTime(),
      champion: d.champion_accuracy,
      hl14: d.candidates.hl14,
      hl5: d.candidates.hl5,
      win7: d.candidates.win7,
      decision: d.decision === "promote" ? "Promovido" : "Se conserva",
      trigger: TRIGGER_LABEL[d.trigger] ?? d.trigger,
    }));

  const stages = ["collector", "inference", "performance", "retraining"];
  const stats = stages.map((stage) => {
    const rows = runs.filter((r) => r.stage === stage);
    const durations = rows.map((r) => r.duration_s).filter((d): d is number => d != null).sort((a, b) => a - b);
    return {
      stage,
      total: rows.length,
      success: rows.filter((r) => r.status === "success").length,
      skipped: rows.filter((r) => r.status === "skipped").length,
      failed: rows.filter((r) => r.status === "failed").length,
      p50: durations.length ? durations[Math.floor(durations.length / 2)] : null,
    };
  });
  const failures = runs.filter((r) => r.status === "failed").slice(0, 10);
  const promotions = decisions.filter((d) => d.decision === "promote").length;
  const hyper = champion?.hyperparameters ?? {};

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Modelos y pipeline</h1>
          <p>
            Registro de versiones (el joblib vive en Supabase y se espeja en MLflow), cada decisión de reentrenamiento con sus candidatos, y la
            salud de las ejecuciones de GitHub Actions en los últimos 7 días.
          </p>
        </div>
      </div>

      <div className="grid grid-4">
        <Tile label="Versiones registradas" value={models.length} meta={<>{models.filter((m) => m.status === "retired").length} retiradas</>} />
        <Tile label="Decisiones de reentrenamiento" value={decisions.length} meta={<>{promotions} promociones · {decisions.length - promotions} se conserva</>} />
        <Tile
          label="Ejecuciones de etapa (7 días)"
          value={runs.length.toLocaleString("es-CO")}
          badge={failures.length ? <Badge tone="critical">{failures.length} fallas</Badge> : <Badge tone="good">Sin fallas</Badge>}
        />
        <Tile label="Campeón entrenado con datos hasta" value={<span style={{ fontSize: 22 }}>{champion ? fmtDateTime(champion.training_data_end) : "—"}</span>} meta={<>Hora virtual de la competencia</>} />
      </div>

      <div className="grid grid-3" style={{ marginTop: 16 }}>
        <Card
          className="span-2"
          title="Campeón vs. candidatos en cada reentrenamiento"
          sub="Accuracy en ciclos simulados sobre las últimas 24 h, sin fuga de datos. Un candidato solo reemplaza al campeón si lo supera por 0,05 puntos."
        >
          {points.length ? <DecisionChart data={points} /> : <Empty>Sin decisiones.</Empty>}
        </Card>
        <Card title="Modelo en producción" sub="AdaptiveProfileForecaster">
          {champion ? (
            <div style={{ display: "grid", gap: 10 }}>
              <div>
                <div style={{ fontWeight: 650, fontSize: 16 }}>{shortVersion(champion.model_version)}</div>
                <div className="card-sub mono" style={{ overflowWrap: "anywhere" }}>
                  {champion.model_version}
                </div>
              </div>
              <table>
                <tbody>
                  <tr>
                    <td>Perfil</td>
                    <td>Regresión log-lineal por estación (slot × tipo de día)</td>
                  </tr>
                  <tr>
                    <td>Vida media</td>
                    <td className="num">{hyper.train_half_life_days != null ? `${hyper.train_half_life_days} días` : "—"}</td>
                  </tr>
                  <tr>
                    <td>Ventana de nivel</td>
                    <td className="num">{String(hyper.lookback_slots ?? "—")} slots</td>
                  </tr>
                  <tr>
                    <td>Amortiguación</td>
                    <td className="num">{String(hyper.decay ?? "—")}ʰ</td>
                  </tr>
                  <tr>
                    <td>Validación</td>
                    <td className="num">{fmtNumber(champion.validation_metrics.accuracy, 2)}</td>
                  </tr>
                  <tr>
                    <td>Commit</td>
                    <td className="num">
                      <a className="mono" href={`${REPO_URL}/commit/${champion.git_commit}`} target="_blank" rel="noreferrer">
                        {champion.git_commit.slice(0, 7)}
                      </a>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          ) : (
            <Empty>Sin modelo activo.</Empty>
          )}
        </Card>
      </div>

      <Card title="Historial de decisiones" sub="pulso.retraining_decisions · la más reciente primero" className="">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Evaluada</th>
                <th>Corte</th>
                <th>Disparador</th>
                <th>Decisión</th>
                <th className="num">Campeón</th>
                <th className="num">hl14</th>
                <th className="num">hl5</th>
                <th className="num">win7</th>
                <th>Motivo</th>
              </tr>
            </thead>
            <tbody>
              {[...decisions].reverse().map((d) => (
                <tr key={d.decision_id}>
                  <td className="tabular" style={{ whiteSpace: "nowrap" }}>
                    {fmtDateTime(d.evaluated_at)}
                  </td>
                  <td className="tabular" style={{ whiteSpace: "nowrap" }}>
                    {fmtDateTime(d.cutoff)}
                  </td>
                  <td>{TRIGGER_LABEL[d.trigger] ?? d.trigger}</td>
                  <td>{d.decision === "promote" ? <Badge tone="good">Promovido</Badge> : <Badge tone="neutral">Se conserva</Badge>}</td>
                  <td className="num">{fmtNumber(d.champion_accuracy, 2)}</td>
                  <td className="num">{fmtNumber(d.candidates.hl14, 2)}</td>
                  <td className="num">{fmtNumber(d.candidates.hl5, 2)}</td>
                  <td className="num">{fmtNumber(d.candidates.win7, 2)}</td>
                  <td style={{ color: "var(--ink-2)", minWidth: 260 }}>
                    {d.reason}
                    {d.drift_alerts.length > 0 && <div className="card-sub">{d.drift_alerts.join(" · ")}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="grid grid-2" style={{ marginTop: 16 }}>
        <Card title="Versiones del modelo" sub="pulso.model_versions">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Versión</th>
                  <th>Estado</th>
                  <th>Datos hasta</th>
                  <th className="num">Validación</th>
                </tr>
              </thead>
              <tbody>
                {models.map((m) => (
                  <tr key={m.model_version}>
                    <td>
                      <div>{shortVersion(m.model_version)}</div>
                      <div className="card-sub mono">{m.git_commit.slice(0, 7)}</div>
                    </td>
                    <td>
                      <StatusTag status={m.status} />
                    </td>
                    <td className="tabular" style={{ whiteSpace: "nowrap" }}>
                      {fmtDateTime(m.training_data_end)}
                    </td>
                    <td className="num">{fmtNumber(m.validation_metrics.accuracy, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <Card title="Salud del pipeline (7 días)" sub="pulso.pipeline_runs por etapa">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Etapa</th>
                  <th className="num">Ejecuciones</th>
                  <th className="num">OK</th>
                  <th className="num">Omitidas</th>
                  <th className="num">Fallas</th>
                  <th className="num">Duración p50</th>
                </tr>
              </thead>
              <tbody>
                {stats.map((s) => (
                  <tr key={s.stage}>
                    <td>{STAGE_LABEL[s.stage]}</td>
                    <td className="num">{s.total}</td>
                    <td className="num">{s.success}</td>
                    <td className="num">{s.skipped}</td>
                    <td className="num">{s.failed ? <Badge tone="critical">{s.failed}</Badge> : 0}</td>
                    <td className="num">{fmtDuration(s.p50)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ marginTop: 16 }}>
            <div className="card-title" style={{ marginBottom: 8 }}>
              Últimas fallas
            </div>
            {failures.length ? (
              <table>
                <tbody>
                  {failures.map((f) => (
                    <tr key={f.run_id}>
                      <td style={{ whiteSpace: "nowrap" }}>
                        <RunStatusBadge status={f.status} />
                      </td>
                      <td>
                        <div>
                          {STAGE_LABEL[f.stage]} · {fmtAgo(f.started_at, now)}
                          {f.github_run_id && (
                            <>
                              {" · "}
                              <a href={`${REPO_URL}/actions/runs/${f.github_run_id}`} target="_blank" rel="noreferrer">
                                run
                              </a>
                            </>
                          )}
                        </div>
                        <div className="card-sub mono" style={{ overflowWrap: "anywhere" }}>
                          {f.error_message}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="note">Ninguna etapa falló en los últimos 7 días.</div>
            )}
          </div>
        </Card>
      </div>
    </>
  );
}
