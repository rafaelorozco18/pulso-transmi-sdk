"""Etapa 5 · Tracking: versiona en MLflow todo lo que el pipeline decide.

MLflow vive en la nube, en el mismo proyecto de Supabase:
- tracking y Model Registry: PostgreSQL, esquema ``mlflow``, con el rol propio
  ``mlflow_writer`` (``MLFLOW_DATABASE_URL``; su search_path es ``mlflow``);
- artefactos (joblib, JSON de decisiones): Supabase Storage, bucket privado
  ``mlflow``, por su API compatible con S3 (``SUPABASE_ANON_KEY`` y
  ``SUPABASE_SERVICE_ROLE_KEY``).
``MLFLOW_TRACKING_URI`` en el entorno lo reemplaza (p. ej. un sqlite local).

Supabase sigue siendo la fuente de verdad; esta etapa lo espeja de forma
idempotente en cada ciclo (solo lo que aún no tiene ``mlflow_run_id``):

- ``pulso-transmi-models``: un run por versión de modelo con hiperparámetros,
  métricas de validación y el joblib; cada versión queda registrada en
  ``pulso-transmi-forecaster`` y el alias ``champion`` apunta a la activa;
- ``pulso-transmi-retraining``: un run por decisión (disparador, accuracy en
  vivo, drift, promote/keep) con un run hijo por receta candidata y otro para
  el campeón (configuración y accuracy global, por horizonte y por estación);
- ``pulso-transmi-monitoring``: un run por versión servida con la serie de
  accuracy rolling, cobertura, leaderboard y drift (step = snapshot).

    python pipeline/tracking.py              # sincroniza una vez
    python pipeline/tracking.py --ui         # abre la UI de MLflow contra la nube
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

import mlflow  # noqa: E402
import psycopg  # noqa: E402
from mlflow.entities import Metric, Param, RunTag  # noqa: E402
from mlflow.tracking import MlflowClient  # noqa: E402

from bootstrap_supabase import load_dotenv  # noqa: E402
from common import connect, stage  # noqa: E402

REGISTERED_MODEL = "pulso-transmi-forecaster"
ARTIFACT_BUCKET = "mlflow"


class TrackingUnavailable(RuntimeError):
    """Faltan credenciales para MLflow en la nube."""


def _project_ref(database_url: str) -> str:
    ref = os.getenv("SUPABASE_PROJECT_REF")
    if ref:
        return ref
    user = urlsplit(database_url).username or ""
    if "." not in user:
        raise TrackingUnavailable("no se pudo deducir el project ref de Supabase desde DATABASE_URL")
    return user.split(".", 1)[1]


def configure() -> str:
    """Apunta MLflow a Supabase y devuelve la raíz de artefactos."""
    load_dotenv()
    if os.getenv("MLFLOW_TRACKING_URI"):
        mlflow.set_tracking_uri(os.environ["MLFLOW_TRACKING_URI"])
        return os.getenv("MLFLOW_ARTIFACT_ROOT", "")
    database_url = os.getenv("MLFLOW_DATABASE_URL")
    anon, service = os.getenv("SUPABASE_ANON_KEY"), os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not (database_url and anon and service):
        raise TrackingUnavailable("faltan MLFLOW_DATABASE_URL, SUPABASE_ANON_KEY o SUPABASE_SERVICE_ROLE_KEY")
    ref = _project_ref(database_url)
    # El pooler de Supabase ignora `options=-csearch_path`: el esquema lo fija el rol.
    mlflow.set_tracking_uri(database_url.replace("postgresql://", "postgresql+psycopg://", 1).replace("postgres://", "postgresql+psycopg://", 1))
    # S3 de Supabase Storage con token de sesión: access key = ref, secret = anon, token = service_role.
    os.environ.update({
        "MLFLOW_S3_ENDPOINT_URL": f"https://{ref}.storage.supabase.co/storage/v1/s3",
        "AWS_ACCESS_KEY_ID": ref,
        "AWS_SECRET_ACCESS_KEY": anon,
        "AWS_SESSION_TOKEN": service,
        "AWS_DEFAULT_REGION": "us-east-1",
    })
    return f"s3://{ARTIFACT_BUCKET}"


def experiment(client: MlflowClient, name: str, artifact_root: str) -> str:
    found = client.get_experiment_by_name(name)
    if found:
        return found.experiment_id
    location = f"{artifact_root}/{name}" if artifact_root else None
    return client.create_experiment(name, artifact_location=location)


def find_run(client: MlflowClient, experiment_id: str, key: str, value: str):
    runs = client.search_runs([experiment_id], filter_string=f"tags.{key} = '{value}'", max_results=1)
    return runs[0] if runs else None


def _ms(value) -> int:
    return int(value.timestamp() * 1000)


def _score_metrics(prefix: str, accuracy: float | None, by_horizon: dict | None, by_station: dict | None) -> dict[str, float]:
    result = {}
    if isinstance(accuracy, (int, float)):
        result[f"{prefix}accuracy"] = float(accuracy)
    for step, value in (by_horizon or {}).items():
        result[f"{prefix}accuracy_h{int(step) * 15}min"] = float(value)
    for station, value in (by_station or {}).items():
        result[f"{prefix}accuracy_station_{station}"] = float(value)
    return result


def _log(client: MlflowClient, run_id: str, *, metrics: dict[str, float] | None = None, params: dict[str, Any] | None = None,
         tags: dict[str, str] | None = None, timestamp: int | None = None, step: int = 0) -> None:
    ts = timestamp or int(time.time() * 1000)
    client.log_batch(
        run_id,
        metrics=[Metric(key, float(value), ts, step) for key, value in (metrics or {}).items() if value is not None],
        params=[Param(key, str(value)[:6000]) for key, value in (params or {}).items()],
        tags=[RunTag(key, str(value)[:5000]) for key, value in (tags or {}).items()],
    )


# ---------------------------------------------------------------------------
# Modelos y Model Registry
# ---------------------------------------------------------------------------


def sync_models(conn: psycopg.Connection, client: MlflowClient, artifact_root: str) -> int:
    exp = experiment(client, "pulso-transmi-models", artifact_root)
    try:
        client.get_registered_model(REGISTERED_MODEL)
    except mlflow.exceptions.MlflowException:
        client.create_registered_model(REGISTERED_MODEL, description="Perfil log-lineal por estación + corrección y ancla de nivel reciente (Pulso TransMi)")
    rows = conn.execute(
        """
        select v.model_version, v.status, v.git_commit, v.trained_at, v.training_data_end, v.hyperparameters,
               v.validation_metrics, v.feature_set, a.artifact, a.sha256, v.mlflow_run_id
        from pulso.model_versions v join pulso.model_artifacts a using (model_version)
        order by v.trained_at
        """
    ).fetchall()
    created = 0
    for version, status, commit, trained_at, data_end, hyper, metrics, features, blob, digest, run_id in rows:
        if run_id in (None, "", "unsynced"):
            run = find_run(client, exp, "model_version", version)
            if run is None:
                metrics = metrics or {}
                run = client.create_run(exp, run_name=version, start_time=_ms(trained_at), tags={
                    "model_version": version, "git_commit": commit, "model_sha256": digest,
                    "recipe": str(metrics.get("recipe", "")), "source": "supabase:pulso.model_versions"})
                _log(client, run.info.run_id,
                     params={**{k: v for k, v in (hyper or {}).items()}, "training_data_end": data_end.isoformat(),
                             "features": " | ".join((features or {}).get("features", []))},
                     metrics={**_score_metrics("validation_", metrics.get("accuracy"), metrics.get("by_horizon"), metrics.get("by_station")),
                              **({"champion_accuracy_same_window": metrics["champion_accuracy"]}
                                 if isinstance(metrics.get("champion_accuracy"), (int, float)) else {})},
                     timestamp=_ms(trained_at))
                with tempfile.TemporaryDirectory() as tmp:
                    (Path(tmp) / "model.joblib").write_bytes(blob)
                    (Path(tmp) / "validation_metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False))
                    client.log_artifacts(run.info.run_id, tmp, artifact_path="model")
                client.set_terminated(run.info.run_id, end_time=_ms(trained_at))
                created += 1
            run_id = run.info.run_id
            mv = next((m for m in client.search_model_versions(f"name = '{REGISTERED_MODEL}'") if m.run_id == run_id), None)
            if mv is None:
                mv = client.create_model_version(REGISTERED_MODEL, source=f"{run.info.artifact_uri}/model", run_id=run_id,
                                                 tags={"model_version": version, "git_commit": commit})
            conn.execute(
                "update pulso.model_versions set mlflow_run_id = %s, mlflow_model_uri = %s where model_version = %s",
                (run_id, f"models:/{REGISTERED_MODEL}/{mv.version}", version),
            )
        client.set_tag(run_id, "status", status)
        if status == "active":
            uri = conn.execute("select mlflow_model_uri from pulso.model_versions where model_version = %s", (version,)).fetchone()[0]
            if uri:
                client.set_registered_model_alias(REGISTERED_MODEL, "champion", uri.rsplit("/", 1)[1])
    return created


# ---------------------------------------------------------------------------
# Reentrenamiento: run padre por decisión + hijos por candidato
# ---------------------------------------------------------------------------


def sync_decisions(conn: psycopg.Connection, client: MlflowClient, artifact_root: str) -> int:
    exp = experiment(client, "pulso-transmi-retraining", artifact_root)
    rows = conn.execute(
        """select decision_id, evaluated_at, trigger, decision, reason, active_model_version, candidate_model_version,
                  signals, git_commit, github_run_id
           from pulso.retraining_decisions where mlflow_run_id is null order by decision_id"""
    ).fetchall()
    created = 0
    for decision_id, evaluated_at, trigger, decision, reason, active, candidate, signals, commit, gh_run in rows:
        signals = signals or {}
        run = find_run(client, exp, "decision_id", str(decision_id))
        if run is None:
            ts = _ms(evaluated_at)
            candidates = signals.get("candidates") or {}
            best = signals.get("best_candidate") or (max(candidates, key=candidates.get) if candidates else None)
            champion_acc = signals.get("champion_accuracy")
            run = client.create_run(exp, run_name=f"{decision_id:04d}-{trigger}-{decision}", start_time=ts, tags={
                "decision_id": str(decision_id), "trigger": trigger, "decision": decision, "reason": reason,
                "active_model_version": active or "", "candidate_model_version": candidate or "",
                "git_commit": commit or "", "github_run_id": gh_run or "", "data_cutoff": signals.get("cutoff", "")})
            parent = run.info.run_id
            metrics = {f"candidate_{name}": value for name, value in candidates.items()}
            if isinstance(champion_acc, (int, float)):
                metrics["champion_accuracy"] = champion_acc
            if best and isinstance(champion_acc, (int, float)):
                metrics["best_gain_vs_champion"] = candidates[best] - champion_acc
            if isinstance(signals.get("live_accuracy"), (int, float)):
                metrics["live_accuracy_rolling_24h"] = signals["live_accuracy"]
            metrics["drift_alerts"] = len(signals.get("drift_alerts") or [])
            _log(client, parent, metrics=metrics, timestamp=ts, params={
                "trigger": trigger, "best_candidate": best or "", "eval_window_start": signals.get("eval_window_start", ""),
                "data_cutoff": signals.get("cutoff", ""), "accuracy_below": signals.get("accuracy_below", ""),
                "min_gain": signals.get("min_gain", "")})
            client.log_dict(parent, signals, "signals.json")

            children = dict(signals.get("candidate_details") or {})
            if signals.get("champion_details"):
                children["campeón"] = signals["champion_details"]
            for name, detail in children.items():
                is_champion = name == "campeón"
                accuracy = champion_acc if is_champion else candidates.get(name)
                child = client.create_run(exp, run_name=f"{decision_id:04d}-{name}", start_time=ts, tags={
                    "mlflow.parentRunId": parent, "decision_id": f"{decision_id}:{name}", "role": "champion" if is_champion else "candidate",
                    "recipe": name, "selected": str(name == best and decision == "promote").lower(),
                    **({"model_version": detail.get("version", "")} if is_champion else {})})
                _log(client, child.info.run_id, params=detail.get("config") or {}, timestamp=ts,
                     metrics=_score_metrics("", accuracy, detail.get("by_horizon"), detail.get("by_station")))
                client.set_terminated(child.info.run_id, end_time=ts)
            client.set_terminated(parent, end_time=ts)
            created += 1
        conn.execute("update pulso.retraining_decisions set mlflow_run_id = %s where decision_id = %s", (run.info.run_id, decision_id))
    return created


# ---------------------------------------------------------------------------
# Monitoreo en vivo: accuracy, cobertura, leaderboard y drift por corte
# ---------------------------------------------------------------------------

DRIFT_METRICS = ("drift_bias_alert_stations", "drift_psi_alert_stations", "drift_shape_alert_stations",
                 "drift_bias_max_abs", "drift_psi_max", "drift_shape_max")


def sync_monitoring(conn: psycopg.Connection, client: MlflowClient, artifact_root: str) -> int:
    exp = experiment(client, "pulso-transmi-monitoring", artifact_root)
    rows = conn.execute(
        """select snapshot_id, data_cutoff, model_version, accuracy, cycles_submitted, cycles_expected, n_predictions,
                  by_horizon, leaderboard from pulso.performance_snapshots order by snapshot_id"""
    ).fetchall()
    drift = {
        cutoff: values
        for cutoff, *values in conn.execute(
            """select window_end,
                      count(*) filter (where signal = 'residual_bias' and is_alert),
                      count(*) filter (where signal = 'demand_psi' and is_alert),
                      count(*) filter (where signal = 'profile_shape' and is_alert),
                      max(abs(value)) filter (where signal = 'residual_bias'),
                      max(value) filter (where signal = 'demand_psi'),
                      max(value) filter (where signal = 'profile_shape')
               from pulso.drift_signals group by window_end"""
        ).fetchall()
    }
    logged = 0
    runs: dict[str, tuple[str, int]] = {}
    for snapshot_id, cutoff, version, accuracy, submitted, expected, n, by_horizon, board in rows:
        key = version or "sin-modelo"
        if key not in runs:
            run = find_run(client, exp, "model_version", key) or client.create_run(exp, run_name=f"live-{key}", tags={"model_version": key})
            runs[key] = (run.info.run_id, int(run.data.tags.get("last_snapshot_id", 0)))
        run_id, last = runs[key]
        if snapshot_id <= last:
            continue
        values: dict[str, float] = {"coverage_window": submitted / expected if expected else 0.0, "n_predictions": float(n)}
        values |= _score_metrics("live_", accuracy, by_horizon, None)
        for window in ("cumulative", "rolling_24h"):
            row = (board or {}).get(window) or {}
            for field in ("accuracy", "coverage", "rank"):
                if isinstance(row.get(field), (int, float)):
                    values[f"leaderboard_{window}_{field}"] = float(row[field])
        for name, value in zip(DRIFT_METRICS, drift.get(cutoff, ())):
            if value is not None:
                values[name] = float(value)
        _log(client, run_id, metrics=values, tags={"last_snapshot_id": str(snapshot_id)}, timestamp=_ms(cutoff), step=snapshot_id)
        runs[key] = (run_id, snapshot_id)
        logged += 1
    return logged


def sync_all(conn: psycopg.Connection) -> dict[str, int]:
    artifact_root = configure()
    client = MlflowClient()
    return {
        "modelos": sync_models(conn, client, artifact_root),
        "decisiones": sync_decisions(conn, client, artifact_root),
        "snapshots": sync_monitoring(conn, client, artifact_root),
    }


def run(conn: psycopg.Connection) -> dict[str, Any]:
    with stage(conn, "tracking") as details:
        details.update(sync_all(conn))
    return details


def open_ui(port: int) -> None:
    configure()
    subprocess.run([sys.executable, "-m", "mlflow", "ui", "--backend-store-uri", mlflow.get_tracking_uri(), "--port", str(port)], check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ui", action="store_true", help="abre la UI de MLflow contra el tracking en Supabase")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    if args.ui:
        open_ui(args.port)
    else:
        with connect() as connection:
            print(sync_all(connection))
