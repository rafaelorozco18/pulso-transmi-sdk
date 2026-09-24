"""Etapa 2 · Inferencia: ciclo abierto → modelo campeón → predicciones → POST.

Garantías:
- una sola submission aceptada por ciclo (se consulta antes de predecir);
- todo se persiste como ``pending`` ANTES del POST: si la red falla, el
  siguiente sondeo reintenta el mismo payload con la misma Idempotency-Key;
- cada predicción queda ligada a ``model_version`` y ``git_commit``.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from common import Api, Skip, connect, load_active, load_config, read_frame, stage
from pulso_transmi.forecasting import SLOT
from pulso_transmi.submission import build_payload, cycle_targets

MAX_REJECTIONS_PER_CYCLE = 3
HISTORY_HOURS = 6


def record_cycle(conn: psycopg.Connection, cycle: dict[str, Any]) -> None:
    conn.execute(
        """
        insert into pulso.forecast_cycles (cycle_id, opens_at, closes_at, data_cutoff, target_times, raw, fetched_at)
        values (%s, %s, %s, %s, %s, %s, now())
        on conflict (cycle_id) do update set raw = excluded.raw, fetched_at = excluded.fetched_at
        """,
        (cycle["cycle_id"], cycle.get("opens_at"), cycle.get("closes_at"), cycle["data_cutoff"],
         sorted({t["target_at"] for t in cycle.get("targets", [])}), Jsonb(cycle)),
    )


def cycle_submissions(conn: psycopg.Connection, cycle_id: str) -> list[tuple]:
    return conn.execute(
        """
        select s.status, s.idempotency_key, s.payload, s.attempt, s.client_run_id, s.api_submission_id
        from pulso.submissions s join pulso.forecast_runs r using (client_run_id)
        where r.cycle_id = %s order by s.submitted_at desc
        """,
        (cycle_id,),
    ).fetchall()


def prepare(conn: psycopg.Connection, cycle: dict[str, Any], details: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Predice con el campeón y deja forecast_run + predicciones + submission pendiente."""
    model = load_active(conn)
    if model is None:
        raise RuntimeError("no hay modelo activo: ejecuta `python pipeline/retraining.py`")
    cutoff = pd.Timestamp(cycle["data_cutoff"])
    history = read_frame(
        conn,
        "select station_id, observed_at, demand from pulso.observations where observed_at > %s and observed_at <= %s",
        (cutoff - pd.Timedelta(hours=HISTORY_HOURS), cutoff),
    )
    latest = pd.to_datetime(history["observed_at"], utc=True).max() if not history.empty else None
    details["data_lag_slots"] = None if latest is None else int((cutoff - latest) / SLOT)
    targets = cycle_targets(cycle)
    values = model.forecaster.predict_cycle(history, targets, cutoff)

    client_run_id = f"run_{cycle['cycle_id']}_{uuid.uuid4().hex[:12]}"
    payload = build_payload(
        cycle=cycle,
        targets=targets,
        values=values,
        client_run_id=client_run_id,
        model={
            "version": model.version,
            "trained_at": pd.Timestamp(model.trained_at).isoformat(),
            "training_data_end": model.training_data_end.isoformat(),
            "git_commit": model.git_commit,
        },
    )
    idempotency_key = str(uuid.uuid4())
    target_at = pd.to_datetime(targets["target_at"], utc=True)
    records = [
        (client_run_id, station, ts.to_pydatetime(), int(round((ts - cutoff) / SLOT)), float(value),
         float(value) * 0.825, float(value) * 1.213)
        for station, ts, value in zip(targets["station_id"], target_at, values, strict=True)
    ]
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(
            """insert into pulso.forecast_runs (client_run_id, cycle_id, model_version, data_cutoff, git_commit, github_run_id)
               values (%s, %s, %s, %s, %s, %s)""",
            (client_run_id, cycle["cycle_id"], model.version, cutoff.to_pydatetime(), model.git_commit, os.getenv("GITHUB_RUN_ID")),
        )
        cursor.executemany(
            """insert into pulso.predictions (client_run_id, station_id, target_at, horizon_steps, value, lower_bound, upper_bound)
               values (%s, %s, %s, %s, %s, %s, %s)""",
            records,
        )
        cursor.execute(
            "insert into pulso.submissions (client_run_id, idempotency_key, payload, status) values (%s, %s, %s, 'pending')",
            (client_run_id, idempotency_key, Jsonb(payload)),
        )
    details.update({"model_version": model.version, "predictions": len(records)})
    return payload, idempotency_key


def post(conn: psycopg.Connection, api: Api, payload: dict[str, Any], idempotency_key: str, attempt: int) -> tuple[str, dict[str, Any]]:
    try:
        response = api.request("POST", "/v1/submissions", json=payload, headers={"Idempotency-Key": idempotency_key})
        try:
            body = response.json()
        except ValueError:
            body = {"text": response.text[:2000]}
        http_status = response.status_code
        if response.is_success:
            status, error = "accepted", None
        elif http_status == 429 or http_status >= 500:
            status, error = "error", f"HTTP {http_status}: {str(body)[:1500]}"
        else:
            status, error = "rejected", f"HTTP {http_status}: {str(body)[:1500]}"
    except Exception as exc:  # red caída: queda pendiente para reintento
        body, http_status = {"error": str(exc)}, None
        status, error = "error", f"{type(exc).__name__}: {exc}"[:2000]

    with conn.transaction():
        conn.execute(
            """update pulso.submissions set http_status = %s, api_submission_id = %s, status = %s, response = %s,
               error_message = %s, attempt = %s, submitted_at = now() where idempotency_key = %s""",
            (http_status, body.get("submission_id") if status == "accepted" else None, status, Jsonb(body), error, attempt, idempotency_key),
        )
        conn.execute(
            "update pulso.forecast_runs set status = %s, finished_at = now(), error_message = %s where client_run_id = %s",
            ({"accepted": "success", "rejected": "failed"}.get(status, "running"), error, payload["client_run_id"]),
        )
    return status, body


def run(conn: psycopg.Connection, api: Api, cycle: dict[str, Any] | None) -> dict[str, Any]:
    cfg = load_config()["schedule"]
    with stage(conn, "inference", cycle and cycle.get("cycle_id")) as details:
        if cycle is None or cycle.get("state") != "open":
            raise Skip("no hay ciclo abierto")
        seconds_left = (pd.Timestamp(cycle["closes_at"]) - datetime.now(UTC)).total_seconds()
        details["seconds_left"] = round(seconds_left)
        if seconds_left < cfg["submit_margin_seconds"]:
            raise Skip("el ciclo está por cerrar")
        record_cycle(conn, cycle)

        history = cycle_submissions(conn, cycle["cycle_id"])
        if any(row[0] == "accepted" for row in history):
            raise Skip("el ciclo ya tiene una submission aceptada")
        if sum(row[0] == "rejected" for row in history) >= MAX_REJECTIONS_PER_CYCLE:
            raise RuntimeError("la API rechazó este ciclo varias veces; revisar error_message en pulso.submissions")

        retry = next((row for row in history if row[0] in ("pending", "error")), None)
        if retry:
            _, idempotency_key, payload, attempt, _, _ = retry
            attempt += 1
            details.update({"retry": True, "model_version": payload["model"]["version"]})
        else:
            payload, idempotency_key = prepare(conn, cycle, details)
            attempt = 1

        status, body = post(conn, api, payload, idempotency_key, attempt)
        details.update({"status": status, "attempt": attempt, "submission_id": body.get("submission_id")})
        if status != "accepted":
            raise RuntimeError(f"submission {status}: {str(body)[:500]}")
    return details


if __name__ == "__main__":
    with connect() as connection, Api() as client:
        print(run(connection, client, client.current_cycle()))
