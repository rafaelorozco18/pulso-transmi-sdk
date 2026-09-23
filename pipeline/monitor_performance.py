"""Registra desempeño disponible y snapshots del leaderboard tras cada ciclo."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
from psycopg.types.json import Jsonb

from pipeline.bootstrap_supabase import load_dotenv
from pulso_transmi.client import DEFAULT_BASE_URL

ACCURACY_THRESHOLD = 85.0


def main() -> None:
    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("Falta DATABASE_URL")
    base_url = os.getenv("PULSO_API_URL") or DEFAULT_BASE_URL
    headers = {"Authorization": f"Bearer {os.environ['PULSO_API_KEY']}"} if os.getenv("PULSO_API_KEY") else {}
    with httpx.Client(base_url=base_url, headers=headers, timeout=30) as api:
        leaderboards = {window: api.get("/v1/leaderboard", params={"window": window}).json() for window in ("cumulative", "rolling_24h")}
    now = datetime.now(UTC)
    with psycopg.connect(database_url) as connection:
        with connection.transaction(), connection.cursor() as cursor:
            for window, payload in leaderboards.items():
                cursor.execute("insert into pulso.leaderboard_snapshots (window_name, payload) values (%s, %s)", (window, Jsonb(payload)))
            cursor.execute("select model_version, station_id, horizon_steps, n, accuracy from pulso.v_accuracy_by_station")
            scores = cursor.fetchall()
            for model_version, station_id, horizon, n, accuracy in scores:
                if accuracy is None:
                    continue
                cursor.execute(
                    """insert into pulso.drift_signals
                       (window_start, window_end, station_id, signal, value, threshold, is_alert, model_version, details)
                       values (%s, %s, %s, 'wape_rolling', %s, %s, %s, %s, %s)""",
                    (now - timedelta(hours=24), now, station_id, 1 - float(accuracy) / 100, 1 - ACCURACY_THRESHOLD / 100,
                     float(accuracy) < ACCURACY_THRESHOLD, model_version, Jsonb({"accuracy": float(accuracy), "n": n, "horizon_steps": horizon})),
                )
    print(f"desempeño: {len(scores)} grupos evaluados; leaderboard almacenado")


if __name__ == "__main__":
    main()
