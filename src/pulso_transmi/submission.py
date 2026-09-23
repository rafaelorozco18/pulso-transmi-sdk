"""Generación, auditoría y envío idempotente de predicciones a Pulso TransMi."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import joblib
import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from pulso_transmi.baseline import TZ, LogLinearProfileModel
from pulso_transmi.client import DEFAULT_BASE_URL

ROOT = Path(__file__).resolve().parents[2]


def _slot_weekend(ts: pd.Series) -> pd.DataFrame:
    local = pd.to_datetime(ts, utc=True).dt.tz_convert(TZ)
    return pd.DataFrame(
        {
            "slot": local.dt.hour * 4 + local.dt.minute // 15,
            "weekend": (local.dt.dayofweek >= 5).astype(int),
        },
        index=ts.index,
    )


def context_for_targets(context: pd.DataFrame, targets: list[dict[str, Any]]) -> pd.DataFrame:
    """Crea contexto ex ante: lluvia mediana por slot/tipo de día y evento cero.

    El endpoint no publica contexto después de ``data_cutoff``. Para evitar
    fuga de información, la lluvia se imputa con la mediana histórica del mismo
    slot y tipo de día; el target del ciclo actual es medianoche, fuera de las
    ventanas de eventos observadas, por lo que se fija intensidad en cero.
    """
    historical = context.copy()
    keys = _slot_weekend(historical["observed_at"])
    historical = historical.join(keys)
    rain_profile = historical.groupby(["slot", "weekend"], observed=True)["rain_forecast"].median()
    future = pd.DataFrame(targets)
    future["observed_at"] = pd.to_datetime(future.pop("target_at"), utc=True)
    future = future.join(_slot_weekend(future["observed_at"]))
    future["rain_forecast"] = [rain_profile.loc[(slot, weekend)] for slot, weekend in zip(future["slot"], future["weekend"], strict=True)]
    future["event_intensity"] = 0.0
    return future[["station_id", "observed_at", "rain_forecast", "event_intensity"]]


def make_payload(
    *,
    cycle: dict[str, Any],
    model: LogLinearProfileModel,
    manifest: dict[str, Any],
    training_result: dict[str, Any],
    context: pd.DataFrame,
) -> tuple[dict[str, Any], pd.DataFrame]:
    targets = cycle.get("targets", [])
    if cycle.get("state") != "open" or not targets:
        raise ValueError("no hay un ciclo abierto con targets para enviar")
    if len(targets) != cycle.get("expected_predictions"):
        raise ValueError("el número de targets no coincide con expected_predictions")
    features = context_for_targets(context, targets)
    features["value"] = model.predict(features).round(4)
    client_run_id = f"run_{cycle['cycle_id']}_{uuid.uuid4().hex[:16]}"
    predictions = [
        {"station_id": str(row.station_id), "target_at": row.observed_at.isoformat(), "value": float(row.value)}
        for row in features.itertuples(index=False)
    ]
    payload = {
        "schema_version": "1.0",
        "cycle_id": cycle["cycle_id"],
        "client_run_id": client_run_id,
        "data_cutoff": cycle["data_cutoff"],
        "model": {
            "version": manifest["model_version"],
            "trained_at": manifest["created_at"],
            "training_data_end": training_result["cutoff"],
            "git_commit": manifest["git_commit"],
        },
        "predictions": predictions,
    }
    return payload, features


def submit(model_dir: Path) -> dict[str, Any]:
    """Guarda trazabilidad en Supabase y envía una sola submission idempotente."""
    api_key = os.getenv("PULSO_API_KEY")
    database_url = os.getenv("DATABASE_URL")
    if not api_key or not database_url:
        raise RuntimeError("PULSO_API_KEY y DATABASE_URL deben estar configuradas")
    model_dir = model_dir.resolve()
    model = joblib.load(model_dir / "model.joblib")
    if not isinstance(model, LogLinearProfileModel):
        raise TypeError("el joblib no contiene un LogLinearProfileModel")
    manifest = json.loads((model_dir / "manifest.json").read_text())
    training_result = json.loads((model_dir / "training_result.json").read_text())
    metrics = json.loads((model_dir / "metrics.json").read_text())
    base_url = os.getenv("PULSO_API_URL", DEFAULT_BASE_URL).rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=30) as api:
        cycle_response = api.get("/v1/forecast-cycles/current")
        cycle_response.raise_for_status()
        cycle = cycle_response.json()

    if cycle.get("state") != "open":
        return {"skipped": True, "reason": "no hay ciclo abierto", "cycle_id": cycle.get("cycle_id")}

    with psycopg.connect(database_url) as connection:
        previous = connection.execute(
            """
            select s.api_submission_id
            from pulso.submissions s
            join pulso.forecast_runs r using (client_run_id)
            where r.cycle_id = %s and s.status = 'accepted'
            order by s.submitted_at desc limit 1
            """,
            (cycle["cycle_id"],),
        ).fetchone()
    if previous:
        return {
            "skipped": True,
            "reason": "el ciclo ya tiene una submission aceptada",
            "cycle_id": cycle["cycle_id"],
            "submission_id": previous[0],
        }

    with psycopg.connect(database_url) as connection:
        context = pd.read_sql("select observed_at, rain_forecast from pulso.context order by observed_at", connection)
    payload, predictions = make_payload(
        cycle=cycle, model=model, manifest=manifest, training_result=training_result, context=context
    )
    idempotency_key = str(uuid.uuid4())
    now = datetime.now(UTC)
    prediction_records = []
    for row in predictions.itertuples(index=False):
        horizon_steps = int((row.observed_at - pd.Timestamp(cycle["data_cutoff"])).total_seconds() // 900)
        prediction_records.append(
            (
                payload["client_run_id"], str(row.station_id), row.observed_at.to_pydatetime(), horizon_steps,
                float(row.value), float(row.value * 0.825), float(row.value * 1.213),
            )
        )

    with psycopg.connect(database_url) as connection:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                """
                insert into pulso.model_versions (
                  model_version, mlflow_run_id, mlflow_model_uri, git_commit, trained_at, training_data_end,
                  profile_version_id, feature_set, hyperparameters, validation_metrics, status
                ) values (%s, %s, %s, %s, %s, %s,
                  (select profile_version_id from pulso.profile_versions order by profile_version_id desc limit 1),
                  %s, %s, %s, 'candidate')
                on conflict (model_version) do nothing
                """,
                (
                    manifest["model_version"], training_result["run_id"],
                    f"runs:/{training_result['run_id']}/model_package/model.joblib", manifest["git_commit"],
                    manifest["created_at"], training_result["cutoff"], Jsonb({"features": manifest["features"]}),
                    Jsonb({}), Jsonb(metrics),
                ),
            )
            cursor.execute(
                """
                insert into pulso.forecast_cycles (cycle_id, opens_at, closes_at, data_cutoff, target_times, raw, fetched_at)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (cycle_id) do update set raw = excluded.raw, fetched_at = excluded.fetched_at
                """,
                (cycle["cycle_id"], cycle.get("opens_at"), cycle.get("closes_at"), cycle["data_cutoff"],
                 [target["target_at"] for target in cycle["targets"]], Jsonb(cycle), now),
            )
            cursor.execute(
                """
                insert into pulso.forecast_runs (client_run_id, cycle_id, model_version, data_cutoff, git_commit)
                values (%s, %s, %s, %s, %s)
                """,
                (payload["client_run_id"], cycle["cycle_id"], manifest["model_version"], cycle["data_cutoff"], manifest["git_commit"]),
            )
            cursor.executemany(
                """insert into pulso.predictions
                   (client_run_id, station_id, target_at, horizon_steps, value, lower_bound, upper_bound)
                   values (%s, %s, %s, %s, %s, %s, %s)""",
                prediction_records,
            )
            cursor.execute(
                """insert into pulso.submissions (client_run_id, idempotency_key, payload, status)
                   values (%s, %s, %s, 'pending')""",
                (payload["client_run_id"], idempotency_key, Jsonb(payload)),
            )

    try:
        with httpx.Client(base_url=base_url, headers={**headers, "Idempotency-Key": idempotency_key}, timeout=30) as api:
            response = api.post("/v1/submissions", json=payload)
        response_json = response.json()
        response.raise_for_status()
        status, error = "accepted", None
    except (httpx.HTTPError, ValueError) as exc:
        response_json = {"error": str(exc)}
        status, error = "error", f"{type(exc).__name__}: {exc}"[:2000]
        response_status = getattr(getattr(exc, "response", None), "status_code", None)
    else:
        response_status = response.status_code

    with psycopg.connect(database_url) as connection:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                """update pulso.submissions set http_status = %s, api_submission_id = %s, status = %s,
                   response = %s, error_message = %s where idempotency_key = %s""",
                (response_status, response_json.get("submission_id") or response_json.get("id"), status, Jsonb(response_json), error, idempotency_key),
            )
            cursor.execute(
                """update pulso.forecast_runs set status = %s, finished_at = now(), error_message = %s
                   where client_run_id = %s""",
                ("success" if status == "accepted" else "failed", error, payload["client_run_id"]),
            )
            if status == "accepted":
                cursor.execute("update pulso.model_versions set status = 'retired', retired_at = now() where status = 'active'")
                cursor.execute(
                    "update pulso.model_versions set status = 'active', promoted_at = now() where model_version = %s",
                    (manifest["model_version"],),
                )
    if status != "accepted":
        raise RuntimeError(f"submission falló: {error}")
    return {"client_run_id": payload["client_run_id"], "idempotency_key": idempotency_key, "response": response_json, "predictions": len(payload["predictions"])}
