"""Entrena un candidato reproducible, lo usa para el ciclo abierto y lo envía.

Se ejecuta después del colector. La API define los targets futuros del ciclo;
no se predicen las observaciones recién liberadas, pues esas son ground truth.
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import psycopg

from bootstrap_supabase import ROOT, load_dotenv
from pulso_transmi import PulsoTransmiClient
from pulso_transmi.submission import submit
from pulso_transmi.training import train_and_log


def training_data(database_url: str) -> pd.DataFrame:
    with psycopg.connect(database_url) as connection:
        return pd.read_sql(
            """
            select o.station_id, o.observed_at, o.demand, c.rain_forecast, c.event_intensity
            from pulso.observations o
            join pulso.context c using (observed_at)
            order by o.observed_at, o.station_id
            """,
            connection,
        )


def main() -> None:
    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("Falta DATABASE_URL")
    with PulsoTransmiClient() as client:
        meta = client.meta()
    result = train_and_log(training_data(database_url), meta, artifact_root=ROOT / "artifacts" / "models")
    outcome = submit(Path(result.package_path))
    print(f"modelo {result.model_version} · validación {result.accuracy:.2f}")
    print(outcome)


if __name__ == "__main__":
    main()
