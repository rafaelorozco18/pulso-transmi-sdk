import type { Metadata } from "next";

import { DecisionChart, type DecisionPoint } from "@/components/charts";
import { Badge, Card, Empty, RunStatusBadge, Tile } from "@/components/ui";
import { getClock, getDecisions, getMlflow, getModels, getRecentRuns } from "@/lib/data";
import { fmtAgo, fmtDateTime, fmtDuration, fmtNumber, fmtSigned, shortVersion, STAGE_LABEL, TRIGGER_LABEL } from "@/lib/format";

export const metadata: Metadata = { title: "Modelos y pipeline · Pulso TransMi" };

const REPO_URL = "https://github.com/rafaelorozco18/pulso-transmi-sdk";

function bestOf(candidates: Record<string, number>, stored: string | null): string | null {
  if (stored && stored in candidates) return stored;
  const entries = Object.entries(candidates);
  return entries.length ? entries.reduce((a, b) => (b[1] > a[1] ? b : a))[0] : null;
}

function StatusTag({ status }: { status: string }) {
  if (status === "active") return <Badge tone="good">Activo</Badge>;
  if (status === "retired") return <Badge tone="neutral">Retirado</Badge>;
  if (status === "rejected") return <Badge tone="critical">Rechazado</Badge>;
  return <Badge tone="neutral">Candidato</Badge>;
}

export default async function ModelsPage() {
  const [models, decisions, runs, clock, mlflow] = await Promise.all([getModels(), getDecisions(), getRecentRuns(), getClock(), getMlflow()]);
  const now = clock.now;
  const champion = models.find((m) => m.status === "active");

  const points: DecisionPoint[] = decisions
    .filter((d) => d.cutoff)
    .map((d) => {
      const best = bestOf(d.candidates, d.best_candidate);
      return {
        ts: new Date(d.cutoff!).getTime(),
        champion: d.champion_accuracy,
        best: best ? d.candidates[best] : null,
        bestName: best,
        live: d.live_accuracy,
        promoted: d.decision === "promote",
        decision: d.decision === "promote" ? "Promovido" : "Se conserva",
        trigger: TRIGGER_LABEL[d.trigger] ?? d.trigger,
        candidates: d.candidates,
      };
    });
  const latestDecision = decisions.at(-1);
  const policy = [...decisions].reverse().find((d) => d.accuracy_below != null);
  const recipes = Object.keys(latestDecision?.candidates ?? {});
  const evalHours = latestDecision?.eval_hours != null ? `${fmtNumber(latestDecision.eval_hours, 0)} h` : "la ventana de evaluación";
  const mlflowRun = new Map(mlflow.retraining.map((r) => [r.decision_id, r]));

  const stages = ["collector", "inference", "performance", "retraining", "tracking"];
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
            En cada ciclo (≈ cada hora) el pipeline mide la accuracy de lo enviado y el drift; si la accuracy cae bajo el umbral mínimo o hay
            drift, reentrena todas las recetas candidatas, las valida contra el campeón sin fuga de datos y promueve la mejor si gana. Cada
            decisión, candidato y versión queda versionado en MLflow.
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
          title="Accuracy en vivo y respuesta del reentrenamiento"
          sub={`Cuando la accuracy en vivo cae bajo el umbral, el reentrenamiento compara candidatos y campeón en ciclos simulados de las últimas ${evalHours} (sin fuga). Un candidato solo reemplaza al campeón si lo supera por el margen mínimo.`}
        >
          {points.length ? <DecisionChart data={points} threshold={policy?.accuracy_below ?? null} /> : <Empty>Sin decisiones.</Empty>}
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
                    <td>Predicción</td>
                    <td>
                      {Number(hyper.season_slots ?? 0) === 0
                        ? "Perfil log-lineal por estación (slot × tipo de día) + corrección de nivel"
                        : "Promedio de los últimos períodos en el mismo punto del ciclo (perfil diario como respaldo)"}
                    </td>
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
                    <td>Ancla de nivel</td>
                    <td className="num">{hyper.anchor_slots ? `${Number(hyper.anchor_slots) / 4} h` : "sin ancla"}</td>
                  </tr>
                  <tr>
                    <td>Estacionalidad corta</td>
                    <td className="num">
                      {Number(hyper.season_slots ?? 0) === 0
                        ? "no (ciclo diario)"
                        : `${Number(hyper.season_slots) < 0 ? "período detectado" : `${Number(hyper.season_slots) / 4} h`} · ${String(hyper.season_cycles ?? "—")} períodos`}
                    </td>
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

      <div className="grid grid-2" style={{ marginTop: 16 }}>
        <Card title="Política de reentrenamiento automático" sub="pipeline/config.toml · se aplica en cada ciclo de GitHub Actions">
          <ul className="signal-list">
            <li>
              <b>Cuándo</b>
              <span>
                En cada ciclo (≈ cada hora): si la accuracy rolling 24 h de lo enviado cae bajo{" "}
                <b>{policy?.accuracy_below != null ? fmtNumber(policy.accuracy_below, 1) : "el umbral"}</b>, si hay drift de datos, concepto o
                desempeño, o si el campeón lleva 24 h sin evaluarse.
              </span>
            </li>
            <li>
              <b>Qué entrena</b>
              <span>
                {recipes.length} recetas candidatas ({recipes.join(", ")}): memoria del perfil (vida media o ventana), ancla de nivel (a12 = 12 h,
                a24 = 24 h) para drift de tendencia, y estacionalidad corta (seas-k2…k6: período detectado en cada corte, promedio de 2 a 6
                períodos) para cuando la demanda deja de seguir el ciclo diario.
              </span>
            </li>
            <li>
              <b>Cómo valida</b>
              <span>
                Cada receta se entrena solo con datos anteriores a la ventana de {evalHours} y se evalúa en ciclos simulados idénticos a los reales, con
                la métrica oficial; el campeón se evalúa igual.
              </span>
            </li>
            <li>
              <b>Cuándo promueve</b>
              <span>
                Si el mejor candidato supera al campeón por {policy?.min_gain != null ? fmtNumber(policy.min_gain, 2) : "0,05"} puntos: se
                re-entrena con todos los datos, se registra y el siguiente ciclo ya predice con él.
              </span>
            </li>
          </ul>
        </Card>
        <Card title="MLflow" sub="Tracking y Model Registry en Supabase (esquema mlflow); artefactos en Supabase Storage">
          <table>
            <thead>
              <tr>
                <th>Experimento</th>
                <th className="num">Runs</th>
                <th className="num">Runs hijos</th>
                <th>Último</th>
              </tr>
            </thead>
            <tbody>
              {mlflow.experiments.map((e) => (
                <tr key={e.name}>
                  <td className="mono">{e.name}</td>
                  <td className="num">{e.runs}</td>
                  <td className="num">{e.child_runs}</td>
                  <td className="tabular">{fmtAgo(e.last_run_at, now)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="card-title" style={{ margin: "16px 0 8px" }}>
            Model Registry · pulso-transmi-forecaster
          </div>
          <table>
            <thead>
              <tr>
                <th className="num">v</th>
                <th>Versión del modelo</th>
                <th>Alias</th>
                <th>Registrada</th>
              </tr>
            </thead>
            <tbody>
              {mlflow.registry.slice(0, 6).map((v) => (
                <tr key={v.version} className={v.aliases.includes("champion") ? "me" : undefined}>
                  <td className="num">{v.version}</td>
                  <td>{shortVersion(v.model_version)}</td>
                  <td>{v.aliases ? <Badge tone="good">{v.aliases}</Badge> : "—"}</td>
                  <td className="tabular">{fmtDateTime(v.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>

      <Card title="Historial de decisiones" sub="pulso.retraining_decisions · la más reciente primero · cada una es un run en MLflow con un run hijo por candidato" className="">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Evaluada</th>
                <th>Corte</th>
                <th>Disparador</th>
                <th>Decisión</th>
                <th className="num">En vivo</th>
                <th className="num">Campeón</th>
                <th>Mejor candidato</th>
                <th className="num">Ganancia</th>
                <th>Motivo</th>
                <th>MLflow</th>
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
                  <td className="num">{fmtNumber(d.live_accuracy, 2)}</td>
                  <td className="num">{fmtNumber(d.champion_accuracy, 2)}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {(() => {
                      const best = bestOf(d.candidates, d.best_candidate);
                      return best ? `${best} · ${fmtNumber(d.candidates[best], 2)}` : "—";
                    })()}
                  </td>
                  <td className="num">
                    {(() => {
                      const best = bestOf(d.candidates, d.best_candidate);
                      return best && d.champion_accuracy != null ? fmtSigned(d.candidates[best] - d.champion_accuracy, 2) : "—";
                    })()}
                  </td>
                  <td style={{ color: "var(--ink-2)", minWidth: 260 }}>
                    {d.reason}
                    {d.drift_alerts.length > 0 && <div className="card-sub">{d.drift_alerts.join(" · ")}</div>}
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {mlflowRun.has(d.decision_id) ? (
                      <span className="card-sub mono" title={`run ${mlflowRun.get(d.decision_id)!.run_id}`}>
                        ✓ {mlflowRun.get(d.decision_id)!.candidates} runs hijos
                      </span>
                    ) : (
                      <span className="card-sub">pendiente</span>
                    )}
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
