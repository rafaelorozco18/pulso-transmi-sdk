"""Piezas compartidas por las etapas del pipeline MLOps.

- configuración (``config.toml``) y variables de entorno;
- cliente HTTP autenticado con reintentos para 429/5xx;
- bitácora de cada etapa en ``pulso.pipeline_runs``;
- registro de modelos servibles en ``pulso.model_versions`` + ``pulso.model_artifacts``.
"""

from __future__ import annotations

import io
import os
import subprocess
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator

import httpx
import joblib
import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from bootstrap_supabase import ROOT, load_dotenv
from pulso_transmi.client import DEFAULT_BASE_URL
from pulso_transmi.forecasting import AdaptiveProfileForecaster

CONFIG_PATH = Path(__file__).with_name("config.toml")
MODEL_FAMILY = "adaptive-profile"


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)


def env(name: str) -> str:
    load_dotenv()
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Falta la variable de entorno {name}")
    return value


def git_commit() -> str:
    if os.getenv("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "0000000"


def connect() -> psycopg.Connection:
    return psycopg.connect(env("DATABASE_URL"), autocommit=True)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class Api:
    """Cliente de la API de competencia con reintentos acotados."""

    def __init__(self) -> None:
        load_dotenv()
        base_url = (os.getenv("PULSO_API_URL") or DEFAULT_BASE_URL).rstrip("/")
        headers = {"User-Agent": "pulso-transmi-pipeline/1.0"}
        if os.getenv("PULSO_API_KEY"):
            headers["Authorization"] = f"Bearer {os.environ['PULSO_API_KEY']}"
        self.http = httpx.Client(base_url=base_url, headers=headers, timeout=30, follow_redirects=True)

    def __enter__(self) -> "Api":
        return self

    def __exit__(self, *args: object) -> None:
        self.http.close()

    def request(self, method: str, path: str, *, attempts: int = 4, **kwargs: Any) -> httpx.Response:
        for attempt in range(1, attempts + 1):
            try:
                response = self.http.request(method, path, **kwargs)
            except httpx.TransportError:
                if attempt == attempts:
                    raise
            else:
                if response.status_code != 429 and response.status_code < 500:
                    return response
                if attempt == attempts:
                    return response
            time.sleep(min(2 ** attempt, 10))
        raise AssertionError("unreachable")

    def current_cycle(self) -> dict[str, Any] | None:
        """Ciclo abierto o ``None``: la API responde 404 entre ciclos."""
        response = self.request("GET", "/v1/forecast-cycles/current")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def leaderboard(self, window: str) -> dict[str, Any]:
        response = self.request("GET", "/v1/leaderboard", params={"window": window})
        response.raise_for_status()
        return response.json()

    def me(self) -> dict[str, Any]:
        response = self.request("GET", "/v1/me")
        response.raise_for_status()
        return response.json()


# ---------------------------------------------------------------------------
# Bitácora de etapas
# ---------------------------------------------------------------------------


class Skip(Exception):
    """La etapa no tenía trabajo pendiente; se registra como ``skipped``."""


@contextmanager
def stage(conn: psycopg.Connection, name: str, cycle_id: str | None = None) -> Iterator[dict[str, Any]]:
    run_id = conn.execute(
        "insert into pulso.pipeline_runs (stage, cycle_id, git_commit, github_run_id) values (%s, %s, %s, %s) returning run_id",
        (name, cycle_id, git_commit(), os.getenv("GITHUB_RUN_ID")),
    ).fetchone()[0]
    details: dict[str, Any] = {}
    started = time.monotonic()
    try:
        yield details
    except Skip as reason:
        details.setdefault("reason", str(reason))
        _finish(conn, run_id, "skipped", details, None)
        print(f"[{name}] omitida: {reason}")
    except Exception as exc:
        _finish(conn, run_id, "failed", details, f"{type(exc).__name__}: {exc}"[:2000])
        raise
    else:
        details["duration_s"] = round(time.monotonic() - started, 2)
        _finish(conn, run_id, "success", details, None)
        print(f"[{name}] ok {_short(details)}")


def _finish(conn: psycopg.Connection, run_id: int, status: str, details: dict[str, Any], error: str | None) -> None:
    conn.execute(
        "update pulso.pipeline_runs set status = %s, finished_at = now(), details = %s, error_message = %s where run_id = %s",
        (status, Jsonb(details), error, run_id),
    )


def _short(details: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in details.items() if not isinstance(v, (dict, list)))


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------


def read_frame(conn: psycopg.Connection, query: str, params: tuple = ()) -> pd.DataFrame:
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        columns = [column.name for column in cursor.description]
        return pd.DataFrame(cursor.fetchall(), columns=columns)


def training_frame(conn: psycopg.Connection, until: datetime | pd.Timestamp | None = None) -> pd.DataFrame:
    """Observaciones con su contexto (el stream no trae contexto: queda nulo)."""
    frame = read_frame(
        conn,
        """
        select o.station_id, o.observed_at, o.demand, c.rain_forecast, c.event_intensity
        from pulso.observations o
        left join pulso.context c using (observed_at)
        where %s::timestamptz is null or o.observed_at <= %s::timestamptz
        order by o.observed_at, o.station_id
        """,
        (until, until),
    )
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
    for column in ("demand", "rain_forecast", "event_intensity"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def latest_observed_at(conn: psycopg.Connection) -> pd.Timestamp | None:
    value = conn.execute("select max(observed_at) from pulso.observations").fetchone()[0]
    return None if value is None else pd.Timestamp(value)


# ---------------------------------------------------------------------------
# Registro de modelos
# ---------------------------------------------------------------------------


@dataclass
class ServedModel:
    version: str
    forecaster: AdaptiveProfileForecaster
    training_data_end: pd.Timestamp
    trained_at: datetime
    git_commit: str
    validation_metrics: dict[str, Any]


def new_version_name(recipe: str) -> str:
    return f"{MODEL_FAMILY}-{recipe}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{git_commit()[:7]}"


def register_model(
    conn: psycopg.Connection,
    *,
    version: str,
    forecaster: AdaptiveProfileForecaster,
    validation_metrics: dict[str, Any],
    status: str,
) -> None:
    buffer = io.BytesIO()
    joblib.dump(forecaster, buffer)
    blob = buffer.getvalue()
    features = ["station_id", "slot × tipo_de_día (America/Bogota)", "rain_forecast (neutro en inferencia)",
                "event_intensity (neutro en inferencia)", "nivel reciente log(real/perfil)"]
    with conn.transaction():
        conn.execute(
            """
            insert into pulso.model_versions (
              model_version, mlflow_run_id, mlflow_model_uri, git_commit, trained_at, training_data_end,
              feature_set, hyperparameters, validation_metrics, status, promoted_at
            ) values (%s, 'unsynced', null, %s, now(), %s, %s, %s, %s, %s, case when %s = 'active' then now() end)
            """,
            (version, git_commit(), forecaster.training_data_end, Jsonb({"features": features}),
             Jsonb(forecaster.config.as_dict()), Jsonb(validation_metrics), status, status),
        )
        conn.execute(
            "insert into pulso.model_artifacts (model_version, artifact, sha256, size_bytes) values (%s, %s, %s, %s)",
            (version, blob, sha256(blob).hexdigest(), len(blob)),
        )


def promote(conn: psycopg.Connection, version: str) -> None:
    with conn.transaction():
        conn.execute("update pulso.model_versions set status = 'retired', retired_at = now() where status = 'active' and model_version <> %s", (version,))
        conn.execute("update pulso.model_versions set status = 'active', promoted_at = coalesce(promoted_at, now()) where model_version = %s", (version,))


def load_active(conn: psycopg.Connection) -> ServedModel | None:
    row = conn.execute(
        """
        select v.model_version, a.artifact, a.sha256, v.training_data_end, v.trained_at, v.git_commit, v.validation_metrics
        from pulso.model_versions v join pulso.model_artifacts a using (model_version)
        where v.status = 'active'
        order by v.promoted_at desc nulls last limit 1
        """
    ).fetchone()
    if row is None:
        return None
    version, blob, digest, data_end, trained_at, commit, metrics = row
    if sha256(blob).hexdigest() != digest:
        raise RuntimeError(f"el artefacto de {version} no coincide con su sha256")
    forecaster = joblib.load(io.BytesIO(blob))
    if not isinstance(forecaster, AdaptiveProfileForecaster):
        raise TypeError(f"{version} no contiene un AdaptiveProfileForecaster")
    return ServedModel(version, forecaster, pd.Timestamp(data_end), trained_at, commit, metrics or {})
