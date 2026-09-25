"""Recalcula el data drift (demand_psi, profile_shape) de cortes ya evaluados.

Las señales de ``drift.py`` se agregaron después de que el pipeline empezó a
correr. Este script las calcula para cada corte que ya tiene ``residual_bias``
y aún no tiene ``demand_psi``, con el mismo código y la misma ventana que la
etapa de desempeño, y el campeón que estaba activo. Es idempotente.

    python pipeline/backfill_drift.py
"""

from __future__ import annotations

import pandas as pd
from psycopg.types.json import Jsonb

from common import connect, load_active, load_config, read_frame
from drift import data_drift_signals


def main() -> None:
    cfg = load_config()["performance"]
    window = pd.Timedelta(hours=cfg["window_hours"])
    with connect() as conn:
        champion = load_active(conn)
        if champion is None:
            raise SystemExit("no hay modelo activo")
        cutoffs = [row[0] for row in conn.execute(
            """select distinct window_end from pulso.drift_signals b
               where signal = 'residual_bias' and model_version = %s
                 and not exists (select 1 from pulso.drift_signals p
                                 where p.signal = 'demand_psi' and p.window_end = b.window_end)
               order by 1""",
            (champion.version,),
        ).fetchall()]
        reference = read_frame(
            conn,
            "select station_id, observed_at, demand from pulso.observations where observed_at > %s and observed_at <= %s",
            (champion.training_data_end - pd.Timedelta(days=cfg["reference_days"]), champion.training_data_end),
        )
        reference["demand"] = pd.to_numeric(reference["demand"])
        for cutoff in cutoffs:
            start = pd.Timestamp(cutoff) - window
            observed = read_frame(
                conn,
                "select station_id, observed_at, demand from pulso.observations where observed_at > %s and observed_at <= %s",
                (start, cutoff),
            )
            observed["demand"] = pd.to_numeric(observed["demand"])
            rows = data_drift_signals(observed, reference, champion.forecaster.base_prediction, cfg)
            with conn.transaction(), conn.cursor() as cursor:
                cursor.executemany(
                    """insert into pulso.drift_signals (window_start, window_end, station_id, signal, value, threshold, is_alert, model_version, details)
                       values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [(start, cutoff, station, signal, value, threshold, alert, champion.version, Jsonb({**extra, "backfill": True}))
                     for station, signal, value, threshold, alert, extra in rows],
                )
            flagged = sum(1 for _, signal, _, _, alert, _ in rows if signal == "demand_psi" and alert)
            print(f"{pd.Timestamp(cutoff):%Y-%m-%d %H:%M} · {len(rows)} señales · PSI en alerta: {flagged}")


if __name__ == "__main__":
    main()
