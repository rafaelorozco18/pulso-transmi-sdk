"""Etapa 1 · Colector: API /v1/stream/observations → validación → PostgreSQL.

Idempotente: la llave primaria (station_id, observed_at) y el upsert evitan
duplicados; ``ingestion_runs`` registra recibidas / insertadas / actualizadas.
"""

from __future__ import annotations

import os

import pandas as pd
import psycopg

from bootstrap_supabase import fetch_stream, git_commit, load_stream
from common import connect, stage

EXPECTED_COLUMNS = {"station_id", "observed_at", "demand"}


def validate(observations: pd.DataFrame, stations: set[str]) -> dict[str, int]:
    """Rechaza el lote completo si viola el contrato; no inserta datos dudosos."""
    if observations.empty:
        return {"rows": 0}
    missing = EXPECTED_COLUMNS - set(observations.columns)
    if missing:
        raise ValueError(f"faltan columnas en el stream: {sorted(missing)}")
    frame = observations.copy()
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True, errors="coerce")
    frame["demand"] = pd.to_numeric(frame["demand"], errors="coerce")
    problems = {
        "timestamp_invalido": int(frame["observed_at"].isna().sum()),
        "timestamp_no_alineado": int((frame["observed_at"].dt.minute % 15 != 0).sum()),
        "demanda_invalida": int((frame["demand"].isna() | (frame["demand"] < 0) | (frame["demand"] % 1 != 0)).sum()),
        "estacion_desconocida": int((~frame["station_id"].astype(str).isin(stations)).sum()),
        "duplicados": int(frame.duplicated(["station_id", "observed_at"]).sum()),
    }
    bad = {name: count for name, count in problems.items() if count}
    if bad:
        raise ValueError(f"lote inválido del stream: {bad}")
    per_slot = frame.groupby("observed_at")["station_id"].nunique()
    return {"rows": len(frame), "slots_incompletos": int((per_slot < len(stations)).sum())}


def run(conn: psycopg.Connection) -> dict:
    with stage(conn, "collector") as details:
        api = fetch_stream()
        stations = {row[0] for row in conn.execute("select station_id from pulso.stations").fetchall()}
        details.update(validate(api["observations"], stations))
        run_id = conn.execute(
            """
            insert into pulso.ingestion_runs (source, status, api_version, server_time, git_commit, github_run_id)
            values ('stream_observations', 'running', %s, %s, %s, %s) returning run_id
            """,
            (api["meta"]["api_version"], api["server_time"], git_commit(), os.getenv("GITHUB_RUN_ID")),
        ).fetchone()[0]
        try:
            result = load_stream(conn, api, run_id)
        except Exception as exc:
            conn.execute(
                "update pulso.ingestion_runs set status = 'failed', finished_at = now(), error_message = %s where run_id = %s",
                (f"{type(exc).__name__}: {exc}"[:2000], run_id),
            )
            raise
        inserted, updated = result["counts"]["observations"]
        latest = conn.execute("select max(observed_at) from pulso.observations").fetchone()[0]
        details.update({"ingestion_run_id": run_id, "inserted": inserted, "updated": updated,
                        "max_observed_at": latest.isoformat() if latest else None})
    return details


if __name__ == "__main__":
    with connect() as connection:
        print(run(connection))
