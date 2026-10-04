"""Etapa 1 · Colector: API /v1/stream/observations → validación → PostgreSQL.

Idempotente: la llave primaria (station_id, observed_at) y el upsert evitan
duplicados; ``ingestion_runs`` registra recibidas / insertadas / actualizadas.

La API cambia de contrato sin aviso (0.9.0: ``schema_version`` 2 mueve la
demanda a ``measurement = {value: "546.00", unit, quality}``). ``normalize``
lleva cada versión conocida a la columna ``demand`` y ``validate`` pone en
cuarentena las filas que violan el contrato en vez de tirar el lote entero:
rechazarlo dejaba al modelo prediciendo con datos congelados.
"""

from __future__ import annotations

import os

import pandas as pd
import psycopg

from bootstrap_supabase import fetch_stream, git_commit, load_stream
from common import connect, stage

EXPECTED_COLUMNS = {"station_id", "observed_at"}
KNOWN_UNITS = {"passengers": 1.0}
# Si al menos esta fracción de las filas nuevas es inválida, el contrato cambió
# de una forma que no conocemos: se falla en voz alta en vez de ingerir a medias.
MAX_INVALID_NEW_SHARE = 0.5


def normalize(observations: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Lleva las filas de cualquier ``schema_version`` conocida a ``demand``; quita las ``missing``."""
    frame = observations.copy()
    if "demand" not in frame.columns:
        frame["demand"] = pd.NA
    frame["demand"] = pd.to_numeric(frame["demand"], errors="coerce")
    stats: dict[str, int] = {}
    if "measurement" in frame.columns:
        nested = frame["measurement"].map(lambda m: isinstance(m, dict))
        if nested.any():
            measurement = pd.json_normalize(frame.loc[nested, "measurement"].tolist()).set_index(frame.index[nested])
            unit = measurement.get("unit", pd.Series("passengers", index=measurement.index)).fillna("passengers")
            unknown = sorted(set(unit) - set(KNOWN_UNITS))
            if unknown:
                raise ValueError(f"unidad desconocida en el stream: {unknown}")
            quality = measurement.get("quality", pd.Series("observed", index=measurement.index)).fillna("observed")
            value = pd.to_numeric(measurement.get("value"), errors="coerce") * unit.map(KNOWN_UNITS)
            missing = quality.eq("missing") | value.isna()
            fill = frame["demand"].isna() & nested
            frame.loc[fill, "demand"] = value.reindex(frame.index[fill])
            drop = missing.index[missing & frame.loc[missing.index, "demand"].isna()]
            frame = frame.drop(index=drop)
            stats = {"filas_v2": int(nested.sum()), "faltantes_api": len(drop)}
    return frame, stats


def validate(observations: pd.DataFrame, stations: set[str], since: pd.Timestamp | None = None) -> tuple[pd.DataFrame, dict]:
    """Devuelve el lote limpio y el detalle; cuarentena por fila, falla si se rompió el contrato."""
    if observations.empty:
        return observations, {"rows": 0}
    missing = EXPECTED_COLUMNS - set(observations.columns)
    if missing:
        raise ValueError(f"faltan columnas en el stream: {sorted(missing)}")
    frame, stats = normalize(observations)
    frame["station_id"] = frame["station_id"].astype(str)
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True, errors="coerce")
    problems = {
        "timestamp_invalido": frame["observed_at"].isna(),
        "timestamp_no_alineado": frame["observed_at"].dt.minute % 15 != 0,
        "demanda_invalida": frame["demand"].isna() | (frame["demand"] < 0) | (frame["demand"] % 1 != 0),
        "estacion_desconocida": ~frame["station_id"].isin(stations),
        "duplicados": frame.duplicated(["station_id", "observed_at"], keep="last"),
    }
    invalid = pd.concat(problems, axis=1).any(axis=1)
    new = frame["observed_at"].isna() | (frame["observed_at"] > since if since is not None else True)
    bad = {name: int(mask.sum()) for name, mask in problems.items() if mask.any()}
    if invalid[new].sum() >= MAX_INVALID_NEW_SHARE * max(int(new.sum()), 1):
        raise ValueError(f"lote inválido del stream: {bad}")
    clean = frame.loc[~invalid].assign(demand=lambda f: f["demand"].astype(int))
    per_slot = clean.groupby("observed_at")["station_id"].nunique()
    details = {"rows": len(clean), "slots_incompletos": int((per_slot < len(stations)).sum()), **stats}
    if bad:
        details["descartadas"] = bad
    return clean, details


def run(conn: psycopg.Connection) -> dict:
    with stage(conn, "collector") as details:
        api = fetch_stream()
        stations = {row[0] for row in conn.execute("select station_id from pulso.stations").fetchall()}
        since = conn.execute("select max(observed_at) from pulso.observations").fetchone()[0]
        api["observations"], checked = validate(api["observations"], stations, since)
        details.update(checked)
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
