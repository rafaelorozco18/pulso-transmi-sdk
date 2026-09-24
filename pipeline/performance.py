"""Etapa 3 · Desempeño: predicciones enviadas vs. demanda real + señales de drift.

Por cada nuevo corte de datos guarda en ``performance_snapshots``:
- accuracy oficial rolling (WAPE por estación, promedio simple), por estación y
  por horizonte, del modelo que realmente se envió;
- cobertura: ciclos aceptados / ciclos esperados en la ventana;
- la fila propia del leaderboard (cumulative y rolling_24h).

Y en ``drift_signals``:
- ``wape_rolling``: desempeño real de la red;
- ``residual_bias``: log(real / perfil del campeón) medio por estación
  (cambio de nivel = concept drift que el perfil ya no explica);
- ``data_quality``: slots faltantes en la ventana.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from common import Api, Skip, connect, latest_observed_at, load_active, load_config, read_frame, stage
from pulso_transmi.forecasting import official_accuracy


def own_leaderboard_rows(api: Api) -> dict[str, Any]:
    try:
        name = api.me().get("display_name")
        rows = {}
        for window in ("cumulative", "rolling_24h"):
            board = api.leaderboard(window).get("data", [])
            rows[window] = next((row for row in board if row.get("display_name") == name), None)
        return rows
    except Exception as exc:  # el leaderboard no debe tumbar el monitoreo
        return {"error": str(exc)[:300]}


def run(conn: psycopg.Connection, api: Api) -> dict[str, Any]:
    config = load_config()
    cfg = config["performance"]
    cycle_every = pd.Timedelta(minutes=config["schedule"]["cycle_every_minutes"])
    with stage(conn, "performance") as details:
        cutoff = latest_observed_at(conn)
        if cutoff is None:
            raise Skip("no hay observaciones")
        if conn.execute("select 1 from pulso.performance_snapshots where data_cutoff = %s", (cutoff,)).fetchone():
            raise Skip("este corte ya fue evaluado")
        start = cutoff - pd.Timedelta(hours=cfg["window_hours"])
        details["data_cutoff"] = cutoff.isoformat()

        errors = read_frame(
            conn,
            """
            select distinct on (r.cycle_id, p.station_id, p.target_at)
                   r.cycle_id, r.model_version, p.station_id, p.target_at, p.horizon_steps,
                   p.value as prediction, o.demand
            from pulso.predictions p
            join pulso.forecast_runs r using (client_run_id)
            join pulso.submissions s using (client_run_id)
            join pulso.observations o on o.station_id = p.station_id and o.observed_at = p.target_at
            where s.status = 'accepted' and p.target_at > %s and p.target_at <= %s
            order by r.cycle_id, p.station_id, p.target_at, s.submitted_at desc
            """,
            (start, cutoff),
        )
        for column in ("prediction", "demand"):
            errors[column] = pd.to_numeric(errors[column])
        score = official_accuracy(errors)

        resolved = pd.date_range(start.ceil(cycle_every), cutoff - pd.Timedelta(hours=1), freq=cycle_every)
        submitted = conn.execute(
            """select count(distinct r.cycle_id) from pulso.forecast_runs r join pulso.submissions s using (client_run_id)
               where s.status = 'accepted' and r.data_cutoff >= %s and r.data_cutoff <= %s""",
            (start, cutoff - pd.Timedelta(hours=1)),
        ).fetchone()[0]

        champion = load_active(conn)
        model_version = champion.version if champion else None
        alerts: list[str] = []
        signals: list[tuple] = []
        n = score["n"]
        if score["accuracy"] is not None:
            is_alert = n >= cfg["min_predictions"] and score["accuracy"] < cfg["accuracy_alert"]
            signals.append((None, "wape_rolling", 1 - score["accuracy"] / 100, 1 - cfg["accuracy_alert"] / 100, is_alert,
                            {"accuracy": score["accuracy"], "n": n, "by_horizon": score["by_horizon"]}))
            if is_alert:
                alerts.append(f"accuracy rolling {score['accuracy']:.2f} < {cfg['accuracy_alert']}")

        observed = read_frame(
            conn, "select station_id, observed_at, demand from pulso.observations where observed_at > %s and observed_at <= %s",
            (start, cutoff),
        )
        observed["demand"] = pd.to_numeric(observed["demand"])
        expected_rows = int(cfg["window_hours"] * 4) * max(1, observed["station_id"].nunique())
        signals.append((None, "data_quality", float(expected_rows - len(observed)), 0.0, len(observed) < expected_rows,
                        {"expected_rows": expected_rows, "rows": len(observed)}))

        biased = []
        if champion is not None and not observed.empty:
            base = champion.forecaster.base_prediction(observed["station_id"], observed["observed_at"])
            valid = observed["demand"] > 0
            log_ratio = np.log(observed.loc[valid, "demand"] / base[valid])
            for station_id, bias in log_ratio.groupby(observed.loc[valid, "station_id"]).mean().items():
                alert = abs(bias) > cfg["bias_alert"]
                biased += [station_id] if alert else []
                signals.append((station_id, "residual_bias", float(bias), cfg["bias_alert"], alert, {"rows": int(valid.sum())}))
        if len(biased) >= cfg["bias_alert_stations"]:
            alerts.append(f"sesgo de nivel en {len(biased)} estaciones: {', '.join(sorted(biased))}")

        leaderboard = own_leaderboard_rows(api)
        with conn.transaction(), conn.cursor() as cursor:
            cursor.execute(
                """insert into pulso.performance_snapshots
                   (data_cutoff, window_hours, model_version, accuracy, cycles_submitted, cycles_expected, n_predictions,
                    by_station, by_horizon, leaderboard)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (cutoff, cfg["window_hours"], model_version, score["accuracy"], submitted, len(resolved), n,
                 Jsonb(score["by_station"]), Jsonb(score["by_horizon"]), Jsonb(leaderboard)),
            )
            cursor.executemany(
                """insert into pulso.drift_signals (window_start, window_end, station_id, signal, value, threshold, is_alert, model_version, details)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(start, cutoff, station, signal, value, threshold, alert, model_version, Jsonb(extra))
                 for station, signal, value, threshold, alert, extra in signals],
            )
        details.update({
            "accuracy": None if score["accuracy"] is None else round(score["accuracy"], 2),
            "n": n, "coverage": f"{submitted}/{len(resolved)}", "alerts": alerts, "drift": bool(alerts),
            "leaderboard_rolling_24h": (leaderboard.get("rolling_24h") or {}).get("accuracy"),
        })
    return details


if __name__ == "__main__":
    with connect() as connection, Api() as client:
        print(run(connection, client))
