"""Carga inicial de Supabase (esquema `pulso`) con el corte publicado por la API.

Descarga stations/observations/context con el SDK (verifica SHA-256 contra
/v1/meta) y los carga de forma idempotente: volver a ejecutarlo no duplica
filas, y si la API cambia un valor ya guardado queda auditado en
`observation_revisions`. Además deja trazabilidad (`dataset_snapshots`,
`ingestion_runs`, `ingestion_cursors`) y las referencias derivadas del corte:
arquetipo por estación, perfil base (`station_profiles`) y calendario.

Uso (desde la raíz del repo; toma DATABASE_URL de .env o del entorno):
    python pipeline/bootstrap_supabase.py
    python pipeline/bootstrap_supabase.py --mlflow-run-id <run del EDA>
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd
import psycopg

from pulso_transmi import PulsoTransmiClient
from pulso_transmi.client import DEFAULT_BASE_URL

ROOT = Path(__file__).resolve().parents[1]
TZ = "America/Bogota"
EVENT_FREE = 0.01  # igual que el EDA: slots con evento por debajo de este umbral forman el perfil base

# Festivos de Colombia desde el inicio del corte hasta fin de 2026 (Ley Emiliani aplicada).
HOLIDAYS = {
    date(2026, 8, 7): "Batalla de Boyacá",
    date(2026, 8, 17): "Asunción de la Virgen",
    date(2026, 10, 12): "Día de la Raza",
    date(2026, 11, 2): "Todos los Santos",
    date(2026, 11, 16): "Independencia de Cartagena",
    date(2026, 12, 8): "Inmaculada Concepción",
    date(2026, 12, 25): "Navidad",
}
CALENDAR_END = date(2026, 12, 31)


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# Datos de la API y derivados
# ---------------------------------------------------------------------------


def fetch_api(workdir: Path) -> dict:
    base_url = os.getenv("PULSO_API_URL", DEFAULT_BASE_URL).rstrip("/")
    with PulsoTransmiClient(base_url=base_url) as client:
        meta = client.meta()
        for filename in ("stations.csv", "observations.csv", "context.csv"):
            client.download(filename, workdir / filename)
    clock = httpx.get(f"{base_url}/v1/clock", timeout=30).json()
    return {
        "meta": meta,
        "server_time": clock.get("server_time"),
        "stations": pd.read_csv(workdir / "stations.csv", dtype={"station_id": "string"}),
        "observations": pd.read_csv(workdir / "observations.csv", dtype={"station_id": "string"}),
        "context": pd.read_csv(workdir / "context.csv"),
    }


def fetch_stream() -> dict:
    """Descarga todas las observaciones ya liberadas por el reloj competitivo."""
    base_url = os.getenv("PULSO_API_URL", DEFAULT_BASE_URL).rstrip("/")
    records: list[dict] = []
    cursor = None
    seen: set[str] = set()
    with PulsoTransmiClient(base_url=base_url) as client:
        meta = client.meta()
        while True:
            page = client.stream_observations_page(cursor=cursor, limit=5000)
            records.extend(page["data"])
            cursor = page.get("next_cursor")
            if cursor is None:
                break
            if cursor in seen:
                raise RuntimeError("la API devolvió un cursor repetido en /v1/stream/observations")
            seen.add(cursor)
    clock = httpx.get(f"{base_url}/v1/clock", timeout=30).json()
    return {
        "meta": meta,
        "server_time": clock.get("server_time"),
        "observations": pd.DataFrame(records),
        "cursor_out": cursor,
    }


def with_calendar_columns(observations: pd.DataFrame, context: pd.DataFrame) -> pd.DataFrame:
    frame = observations.merge(context[["observed_at", "event_intensity"]], on="observed_at", how="left", validate="many_to_one")
    ts = pd.to_datetime(frame["observed_at"], utc=True).dt.tz_convert(TZ)
    frame["slot"] = ts.dt.hour * 4 + ts.dt.minute // 15
    frame["day_type"] = (ts.dt.dayofweek >= 5).map({True: "fin_de_semana", False: "laboral"})
    return frame


def archetypes(frame: pd.DataFrame) -> dict[str, str]:
    """Misma regla que EDA/eda.py (figura 06), sobre el perfil laboral medio."""
    weekday = frame[frame["day_type"] == "laboral"].groupby(["station_id", "slot"])["demand"].mean().unstack()
    result = {}
    for station_id, profile in weekday.iterrows():
        am, midday, pm, night = profile.loc[20:39].max(), profile.loc[44:55].max(), profile.loc[60:75].max(), profile.loc[76:91].max()
        if night > max(am, pm):
            result[station_id] = "ocio_nocturno"
        elif midday > max(am, pm):
            result[station_id] = "universitaria_mediodia"
        elif am / pm > 1.4:
            result[station_id] = "portal_origen_am"
        elif am / pm < 0.85:
            result[station_id] = "oficinas_destino_am"
        else:
            result[station_id] = "intercambio_doble_pico"
    return result


def station_profiles(frame: pd.DataFrame) -> pd.DataFrame:
    clean = frame[frame["event_intensity"] < EVENT_FREE]
    return (
        clean.groupby(["station_id", "day_type", "slot"])["demand"]
        .agg(median="median", mean="mean", p10=lambda s: s.quantile(0.10), p90=lambda s: s.quantile(0.90), n="size")
        .reset_index()
    )


def calendar(start: date, end: date) -> list[tuple]:
    days = []
    day = start
    while day <= end:
        days.append((day, "fin_de_semana" if day.weekday() >= 5 else "laboral", day in HOLIDAYS, HOLIDAYS.get(day)))
        day += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# Carga
# ---------------------------------------------------------------------------


def rows(frame: pd.DataFrame, columns: list[str]) -> Iterable[tuple]:
    return frame[columns].astype(object).itertuples(index=False, name=None)


def upsert(
    cur: psycopg.Cursor,
    table: str,
    columns: list[str],
    data: Iterable[tuple],
    conflict: list[str],
    update: list[str],
) -> tuple[int, int]:
    """COPY a una tabla temporal y upsert. Devuelve (insertadas, actualizadas)."""
    staging = f"staging_{table}"
    cols = ", ".join(columns)
    cur.execute(f"create temp table {staging} (like pulso.{table} including defaults) on commit drop")
    with cur.copy(f"copy {staging} ({cols}) from stdin") as copy:
        for row in data:
            copy.write_row(row)
    changed = " or ".join(f"pulso.{table}.{c} is distinct from excluded.{c}" for c in update)
    assignments = ", ".join(f"{c} = excluded.{c}" for c in update)
    cur.execute(
        f"""
        insert into pulso.{table} ({cols})
        select {cols} from {staging}
        on conflict ({", ".join(conflict)}) do update set {assignments}
        where {changed}
        returning (xmax = 0) as inserted
        """
    )
    flags = [r[0] for r in cur.fetchall()]
    return sum(flags), len(flags) - sum(flags)


def load(conn: psycopg.Connection, api: dict, run_id: int, mlflow_run_id: str | None) -> dict:
    meta, dataset = api["meta"], api["meta"]["dataset"]
    stations, observations, context = api["stations"], api["observations"], api["context"]
    frame = with_calendar_columns(observations, context)
    counts: dict[str, tuple[int, int]] = {}

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            insert into pulso.dataset_snapshots (dataset, api_version, history_start, history_end, files)
            values (%s, %s, %s, %s, %s)
            on conflict (dataset, history_end, api_version) do update set fetched_at = now(), files = excluded.files
            returning snapshot_id
            """,
            (dataset["dataset"], meta["api_version"], dataset["history_start"], dataset["history_end"], psycopg.types.json.Jsonb(dataset["files"])),
        )
        snapshot_id = cur.fetchone()[0]

        stations = stations.assign(archetype=stations["station_id"].map(archetypes(frame)))
        counts["stations"] = upsert(
            cur, "stations", ["station_id", "station_name", "corridor", "latitude", "longitude", "archetype"],
            rows(stations, ["station_id", "station_name", "corridor", "latitude", "longitude", "archetype"]),
            ["station_id"], ["station_name", "corridor", "latitude", "longitude", "archetype"],
        )

        first_day = pd.Timestamp(dataset["history_start"]).date()
        counts["calendar_days"] = upsert(
            cur, "calendar_days", ["day", "day_type", "is_holiday", "holiday_name"],
            calendar(first_day, max(CALENDAR_END, pd.Timestamp(dataset["history_end"]).date())),
            ["day"], ["day_type", "is_holiday", "holiday_name"],
        )

        observations = observations.assign(ingestion_run_id=run_id)
        counts["observations"] = upsert(
            cur, "observations", ["station_id", "observed_at", "demand", "ingestion_run_id"],
            rows(observations, ["station_id", "observed_at", "demand", "ingestion_run_id"]),
            ["station_id", "observed_at"], ["demand"],
        )

        context_columns = ["observed_at", "rain_mm", "rain_forecast", "temperature_c", "temperature_forecast", "event_intensity"]
        counts["context"] = upsert(
            cur, "context", context_columns + ["ingestion_run_id"],
            rows(context.assign(ingestion_run_id=run_id), context_columns + ["ingestion_run_id"]),
            ["observed_at"], context_columns[1:],
        )

        cur.execute(
            """
            select profile_version_id from pulso.profile_versions
            where data_start = %s and data_end = %s and event_free_threshold = %s
            order by profile_version_id limit 1
            """,
            (dataset["history_start"], dataset["history_end"], EVENT_FREE),
        )
        found = cur.fetchone()
        if found:
            profile_version_id = found[0]
            if mlflow_run_id:
                cur.execute("update pulso.profile_versions set mlflow_run_id = %s where profile_version_id = %s", (mlflow_run_id, profile_version_id))
        else:
            cur.execute(
                """
                insert into pulso.profile_versions (data_start, data_end, event_free_threshold, mlflow_run_id)
                values (%s, %s, %s, %s) returning profile_version_id
                """,
                (dataset["history_start"], dataset["history_end"], EVENT_FREE, mlflow_run_id),
            )
            profile_version_id = cur.fetchone()[0]
        profiles = station_profiles(frame).assign(profile_version_id=profile_version_id)
        profile_columns = ["profile_version_id", "station_id", "day_type", "slot", "median", "mean", "p10", "p90", "n"]
        counts["station_profiles"] = upsert(
            cur, "station_profiles", profile_columns, rows(profiles, profile_columns),
            ["profile_version_id", "station_id", "day_type", "slot"], ["median", "mean", "p10", "p90", "n"],
        )

        max_observed_at = max(observations["observed_at"], key=pd.Timestamp)
        for source in ("observations", "context"):
            cur.execute(
                """
                insert into pulso.ingestion_cursors (source, cursor, last_observed_at, last_run_id, updated_at)
                values (%s, null, %s, %s, now())
                on conflict (source) do update
                set last_observed_at = greatest(pulso.ingestion_cursors.last_observed_at, excluded.last_observed_at),
                    last_run_id = excluded.last_run_id, updated_at = now()
                """,
                (source, max_observed_at, run_id),
            )

        received = len(stations) + len(observations) + len(context)
        inserted = sum(c[0] for c in counts.values())
        updated = sum(c[1] for c in counts.values())
        cur.execute(
            """
            update pulso.ingestion_runs
            set status = 'success', finished_at = now(), max_observed_at = %s, snapshot_id = %s,
                rows_received = %s, rows_inserted = %s, rows_updated = %s
            where run_id = %s
            """,
            (max_observed_at, snapshot_id, received, inserted, updated, run_id),
        )
    return {"snapshot_id": snapshot_id, "profile_version_id": profile_version_id, "counts": counts}


def load_stream(conn: psycopg.Connection, api: dict, run_id: int) -> dict:
    """Inserta idempotentemente las observaciones liberadas desde el corte inicial."""
    # Se re-sincroniza todo el stream (es pequeño): el upsert solo escribe filas
    # nuevas o cambiadas, y una revisión de la API queda en observation_revisions.
    observations = api["observations"]
    with conn.transaction(), conn.cursor() as cur:
        if not observations.empty:
            observations = observations.copy()
            observations["observed_at"] = pd.to_datetime(observations["observed_at"], utc=True)
            observations["station_id"] = observations["station_id"].astype("string")

        if observations.empty:
            cur.execute(
                """
                update pulso.ingestion_runs
                set status = 'success', finished_at = now(), cursor_out = %s,
                    rows_received = 0, rows_inserted = 0, rows_updated = 0
                where run_id = %s
                """,
                (api["cursor_out"], run_id),
            )
            return {"counts": {"observations": (0, 0)}}

        counts = {
            "observations": upsert(
                cur,
                "observations",
                ["station_id", "observed_at", "demand", "ingestion_run_id"],
                rows(observations.assign(ingestion_run_id=run_id), ["station_id", "observed_at", "demand", "ingestion_run_id"]),
                ["station_id", "observed_at"],
                ["demand"],
            )
        }
        max_observed_at = max(observations["observed_at"], key=pd.Timestamp)
        cur.execute(
            """
            insert into pulso.ingestion_cursors (source, cursor, last_observed_at, last_run_id, updated_at)
            values ('stream_observations', %s, %s, %s, now())
            on conflict (source) do update
            set cursor = excluded.cursor,
                last_observed_at = greatest(pulso.ingestion_cursors.last_observed_at, excluded.last_observed_at),
                last_run_id = excluded.last_run_id, updated_at = now()
            """,
            (api["cursor_out"], max_observed_at, run_id),
        )
        received = len(observations)
        cur.execute(
            """
            update pulso.ingestion_runs
            set status = 'success', finished_at = now(), cursor_out = %s, max_observed_at = %s,
                rows_received = %s, rows_inserted = %s, rows_updated = %s
            where run_id = %s
            """,
            (api["cursor_out"], max_observed_at, received, counts["observations"][0], counts["observations"][1], run_id),
        )
    return {"counts": counts}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mlflow-run-id", help="run de MLflow del EDA que documenta el perfil base")
    parser.add_argument("--stream", action="store_true", help="ingesta las observaciones liberadas por el reloj competitivo")
    args = parser.parse_args()
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("Falta DATABASE_URL (en .env o en el entorno).")

    with psycopg.connect(database_url, autocommit=True) as conn:
        if args.stream:
            api = fetch_stream()
            source = "stream_observations"
        else:
            with tempfile.TemporaryDirectory() as tmp:
                api = fetch_api(Path(tmp))
            source = "bootstrap_download"
        run_id = conn.execute(
            """
            insert into pulso.ingestion_runs (source, status, api_version, server_time, git_commit, github_run_id)
            values (%s, 'running', %s, %s, %s, %s) returning run_id
            """,
            (source, api["meta"]["api_version"], api["server_time"], git_commit(), os.getenv("GITHUB_RUN_ID")),
        ).fetchone()[0]
        try:
            result = load_stream(conn, api, run_id) if args.stream else load(conn, api, run_id, args.mlflow_run_id)
        except Exception as exc:
            conn.execute(
                "update pulso.ingestion_runs set status = 'failed', finished_at = now(), error_message = %s where run_id = %s",
                (f"{type(exc).__name__}: {exc}"[:2000], run_id),
            )
            raise

    details = f"ingestion_run {run_id}"
    if not args.stream:
        details += f" · snapshot {result['snapshot_id']} · profile_version {result['profile_version_id']}"
    print(details)
    for table, (inserted, updated) in result["counts"].items():
        print(f"  {table:<18} insertadas {inserted:>6}   actualizadas {updated:>6}")


if __name__ == "__main__":
    main()
