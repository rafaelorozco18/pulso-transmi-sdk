import "server-only";

import { iso, query } from "./db";

// ---------------------------------------------------------------------------
// Tipos compartidos con los componentes (todo serializable: fechas en ISO).
// ---------------------------------------------------------------------------

export type Station = {
  station_id: string;
  station_name: string;
  corridor: string;
  latitude: number;
  longitude: number;
  archetype: string | null;
};

export type LeaderboardRow = {
  rank: number | null;
  accuracy: number | null;
  coverage: number | null;
  eligible?: boolean;
  display_name?: string;
  calculated_at?: string;
};

export type Snapshot = {
  computed_at: string;
  data_cutoff: string;
  accuracy: number | null;
  cycles_submitted: number;
  cycles_expected: number;
  n_predictions: number;
  model_version: string | null;
  by_station: Record<string, number>;
  by_horizon: Record<string, number>;
  lb_cumulative: LeaderboardRow | null;
  lb_rolling: LeaderboardRow | null;
};

export type StageStatus = {
  stage: "collector" | "inference" | "performance" | "retraining";
  status: "running" | "success" | "skipped" | "failed";
  started_at: string;
  finished_at: string | null;
  duration_s: number | null;
  cycle_id: string | null;
  details: Record<string, unknown>;
  error_message: string | null;
  github_run_id: string | null;
};

export type Cycle = {
  cycle_id: string;
  data_cutoff: string;
  accepted: boolean;
  accepted_at: string | null;
  attempts: number | null;
  model_version: string | null;
};

export type ModelVersion = {
  model_version: string;
  git_commit: string;
  trained_at: string;
  training_data_end: string;
  hyperparameters: Record<string, unknown>;
  validation_metrics: {
    accuracy?: number;
    recipe?: string;
    by_horizon?: Record<string, number>;
    by_station?: Record<string, number>;
    candidates?: Record<string, number>;
    [key: string]: unknown;
  };
  status: "candidate" | "active" | "retired" | "rejected";
  promoted_at: string | null;
  retired_at: string | null;
};

export type Decision = {
  decision_id: number;
  evaluated_at: string;
  trigger: string;
  decision: string;
  reason: string;
  active_model_version: string | null;
  candidate_model_version: string | null;
  cutoff: string | null;
  candidates: Record<string, number>;
  champion_accuracy: number | null;
  drift_alerts: string[];
  github_run_id: string | null;
};

export type DriftSignal = {
  window_end: string;
  station_id: string | null;
  signal: "residual_bias" | "wape_rolling" | "data_quality" | string;
  value: number;
  threshold: number | null;
  is_alert: boolean;
  details: Record<string, unknown>;
};

export type DataClock = {
  max_observed_at: string | null;
  observations: number;
  last_ingestion_at: string | null;
  /** Momento de la consulta (ms), para textos relativos como "hace 5 min". */
  now: number;
};

// ---------------------------------------------------------------------------
// Consultas base
// ---------------------------------------------------------------------------

type Raw = Record<string, unknown>;

export async function getClock(): Promise<DataClock> {
  const [row] = await query<Raw>("select * from dashboard.data_clock");
  return {
    max_observed_at: iso(row.max_observed_at as Date),
    observations: Number(row.observations),
    last_ingestion_at: iso(row.last_ingestion_at as Date),
    now: Date.now(),
  };
}

export async function getStations(): Promise<Station[]> {
  return query<Station>("select * from dashboard.stations order by station_id");
}

function toSnapshot(row: Raw): Snapshot {
  return {
    computed_at: iso(row.computed_at as Date)!,
    data_cutoff: iso(row.data_cutoff as Date)!,
    accuracy: row.accuracy as number | null,
    cycles_submitted: Number(row.cycles_submitted),
    cycles_expected: Number(row.cycles_expected),
    n_predictions: Number(row.n_predictions),
    model_version: row.model_version as string | null,
    by_station: (row.by_station as Record<string, number>) ?? {},
    by_horizon: (row.by_horizon as Record<string, number>) ?? {},
    lb_cumulative: (row.leaderboard_cumulative as LeaderboardRow) ?? null,
    lb_rolling: (row.leaderboard_rolling_24h as LeaderboardRow) ?? null,
  };
}

/** Un snapshot por corte (el más reciente si se evaluó dos veces), en orden temporal. */
export async function getSnapshots(): Promise<Snapshot[]> {
  const rows = await query<Raw>(
    `select distinct on (data_cutoff) * from dashboard.performance_snapshots
     order by data_cutoff, computed_at desc`,
  );
  return rows.map(toSnapshot);
}

export async function getStageStatus(): Promise<StageStatus[]> {
  const rows = await query<Raw>("select * from dashboard.pipeline_stage_status");
  const order = ["collector", "inference", "performance", "retraining"];
  return rows
    .map((row) => ({
      stage: row.stage as StageStatus["stage"],
      status: row.status as StageStatus["status"],
      started_at: iso(row.started_at as Date)!,
      finished_at: iso(row.finished_at as Date),
      duration_s: row.duration_s as number | null,
      cycle_id: row.cycle_id as string | null,
      details: (row.details as Record<string, unknown>) ?? {},
      error_message: row.error_message as string | null,
      github_run_id: row.github_run_id as string | null,
    }))
    .sort((a, b) => order.indexOf(a.stage) - order.indexOf(b.stage));
}

export async function getCycles(): Promise<Cycle[]> {
  const rows = await query<Raw>("select * from dashboard.cycle_coverage order by data_cutoff");
  return rows.map((row) => ({
    cycle_id: row.cycle_id as string,
    data_cutoff: iso(row.data_cutoff as Date)!,
    accepted: Boolean(row.accepted),
    accepted_at: iso(row.accepted_at as Date),
    attempts: row.attempts == null ? null : Number(row.attempts),
    model_version: row.model_version as string | null,
  }));
}

export async function getModels(): Promise<ModelVersion[]> {
  const rows = await query<Raw>("select * from dashboard.model_versions order by trained_at desc");
  return rows.map((row) => ({
    model_version: row.model_version as string,
    git_commit: row.git_commit as string,
    trained_at: iso(row.trained_at as Date)!,
    training_data_end: iso(row.training_data_end as Date)!,
    hyperparameters: (row.hyperparameters as Record<string, unknown>) ?? {},
    validation_metrics: (row.validation_metrics as ModelVersion["validation_metrics"]) ?? {},
    status: row.status as ModelVersion["status"],
    promoted_at: iso(row.promoted_at as Date),
    retired_at: iso(row.retired_at as Date),
  }));
}

export async function getDecisions(): Promise<Decision[]> {
  const rows = await query<Raw>("select * from dashboard.retraining_decisions order by evaluated_at");
  return rows.map((row) => {
    const signals = (row.signals as Record<string, unknown>) ?? {};
    return {
      decision_id: Number(row.decision_id),
      evaluated_at: iso(row.evaluated_at as Date)!,
      trigger: row.trigger as string,
      decision: row.decision as string,
      reason: row.reason as string,
      active_model_version: row.active_model_version as string | null,
      candidate_model_version: row.candidate_model_version as string | null,
      cutoff: (signals.cutoff as string) ?? null,
      candidates: (signals.candidates as Record<string, number>) ?? {},
      champion_accuracy: (signals.champion_accuracy as number | null) ?? null,
      drift_alerts: (signals.drift_alerts as string[]) ?? [],
      github_run_id: row.github_run_id as string | null,
    };
  });
}

/**
 * Señales de drift por corte de datos. Si un corte se evaluó dos veces queda
 * la última; se descartan filas antiguas cuyo window_end no está en hora virtual.
 */
export async function getDriftSignals(): Promise<DriftSignal[]> {
  const rows = await query<Raw>(
    `select distinct on (signal, coalesce(station_id, ''), window_end)
            window_end, station_id, signal, value, threshold, is_alert, details
     from dashboard.drift_signals
     where window_end <= (select max_observed_at from dashboard.data_clock)
     order by signal, coalesce(station_id, ''), window_end, computed_at desc`,
  );
  return rows
    .map((row) => ({
      window_end: iso(row.window_end as Date)!,
      station_id: row.station_id as string | null,
      signal: row.signal as string,
      value: row.value as number,
      threshold: row.threshold as number | null,
      is_alert: Boolean(row.is_alert),
      details: (row.details as Record<string, unknown>) ?? {},
    }))
    .sort((a, b) => a.window_end.localeCompare(b.window_end));
}

// ---------------------------------------------------------------------------
// Desempeño
// ---------------------------------------------------------------------------

export type SeriesPoint = { t: string; actual?: number; predicted?: number; lower?: number; upper?: number };

/** Demanda real (7 días virtuales) y predicción enviada para una estación y horizonte. */
export async function getStationSeries(stationId: string, horizon: number): Promise<SeriesPoint[]> {
  const [observed, predicted] = await Promise.all([
    query<Raw>(
      "select observed_at, demand from dashboard.observations_recent where station_id = $1 order by observed_at",
      [stationId],
    ),
    query<Raw>(
      `select target_at, predicted, lower_bound, upper_bound from dashboard.prediction_errors
       where station_id = $1 and horizon_steps = $2 order by target_at`,
      [stationId, horizon],
    ),
  ]);
  const points = new Map<string, SeriesPoint>();
  for (const row of observed) {
    const t = iso(row.observed_at as Date)!;
    points.set(t, { t, actual: row.demand as number });
  }
  for (const row of predicted) {
    const t = iso(row.target_at as Date)!;
    const point = points.get(t) ?? { t };
    point.predicted = row.predicted as number;
    point.lower = row.lower_bound as number;
    point.upper = row.upper_bound as number;
    points.set(t, point);
  }
  return [...points.values()].sort((a, b) => a.t.localeCompare(b.t));
}

export type ErrorRow = { station_id: string; horizon: number; pct_error: number };

/** Error porcentual con signo, (predicción − real) / real, en la ventana rolling de 24 h. */
export async function getRecentErrors(): Promise<ErrorRow[]> {
  const rows = await query<Raw>(
    `select station_id, horizon_steps, (predicted - actual) / nullif(actual, 0) as pct_error
     from dashboard.prediction_errors
     where target_at > (select max_observed_at from dashboard.data_clock) - interval '24 hours'
       and actual > 0`,
  );
  return rows.map((row) => ({
    station_id: row.station_id as string,
    horizon: Number(row.horizon_steps),
    pct_error: row.pct_error as number,
  }));
}

// ---------------------------------------------------------------------------
// Pipeline
// ---------------------------------------------------------------------------

export type RunRow = {
  run_id: number;
  stage: string;
  status: string;
  started_at: string;
  duration_s: number | null;
  cycle_id: string | null;
  error_message: string | null;
  github_run_id: string | null;
};

export async function getRecentRuns(): Promise<RunRow[]> {
  const rows = await query<Raw>(
    `select run_id, stage, status, started_at, duration_s, cycle_id, error_message, github_run_id
     from dashboard.pipeline_runs where started_at > now() - interval '7 days' order by started_at desc`,
  );
  return rows.map((row) => ({
    run_id: Number(row.run_id),
    stage: row.stage as string,
    status: row.status as string,
    started_at: iso(row.started_at as Date)!,
    duration_s: row.duration_s as number | null,
    cycle_id: row.cycle_id as string | null,
    error_message: row.error_message as string | null,
    github_run_id: row.github_run_id as string | null,
  }));
}

// ---------------------------------------------------------------------------
// Leaderboard (anonimizado en la vista: solo se conoce el nombre propio)
// ---------------------------------------------------------------------------

export type BoardRow = {
  rank: number;
  kind: string;
  accuracy: number;
  coverage: number;
  is_me: boolean;
  display_name: string | null;
};

export async function getLeaderboard(window: "rolling_24h" | "cumulative"): Promise<{ fetched_at: string | null; rows: BoardRow[] }> {
  const rows = await query<Raw>(
    "select fetched_at, rank, kind, accuracy, coverage, is_me, display_name from dashboard.leaderboard where window_name = $1 order by rank, accuracy desc",
    [window],
  );
  return {
    fetched_at: rows.length ? iso(rows[0].fetched_at as Date) : null,
    rows: rows.map((row) => ({
      rank: Number(row.rank),
      kind: row.kind as string,
      accuracy: row.accuracy as number,
      coverage: row.coverage as number,
      is_me: Boolean(row.is_me),
      display_name: row.display_name as string | null,
    })),
  };
}
