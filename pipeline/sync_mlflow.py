"""Replica en MLflow lo que el pipeline registra en Supabase (fuente de verdad).

Los runners de GitHub Actions son efímeros, así que el pipeline guarda modelos,
decisiones y desempeño en PostgreSQL. Este script lo espeja, de forma
idempotente, en el tracking de MLflow (por defecto ``sqlite:///mlflow.db``):

- ``pulso-transmi-models``: un run por versión de modelo, con hiperparámetros,
  métricas de validación (global, por estación, por horizonte) y el joblib;
  cada versión queda en el Model Registry ``pulso-transmi-forecaster`` y el
  alias ``champion`` apunta a la versión activa;
- ``pulso-transmi-retraining``: un run por decisión (trigger, candidatos,
  campeón, promote/keep);
- ``pulso-transmi-monitoring``: un run por versión servida con la serie de
  accuracy rolling, cobertura, leaderboard y drift (estaciones en alerta y
  máximos de PSI, forma y sesgo por corte; step = snapshot).

    python pipeline/sync_mlflow.py
    mlflow ui --backend-store-uri sqlite:///mlflow.db
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

import mlflow  # noqa: E402
from mlflow.tracking import MlflowClient  # noqa: E402

from common import ROOT, connect  # noqa: E402

REGISTERED_MODEL = "pulso-transmi-forecaster"


def experiment(client: MlflowClient, name: str) -> str:
    found = client.get_experiment_by_name(name)
    if found:
        return found.experiment_id
    return client.create_experiment(name, artifact_location=(ROOT / "mlartifacts").as_uri())


def find_run(client: MlflowClient, experiment_id: str, key: str, value: str):
    runs = client.search_runs([experiment_id], filter_string=f"tags.{key} = '{value}'", max_results=1)
    return runs[0] if runs else None


def flat_metrics(metrics: dict[str, Any]) -> dict[str, float]:
    result = {}
    if isinstance(metrics.get("accuracy"), (int, float)):
        result["validation_accuracy"] = float(metrics["accuracy"])
    if isinstance(metrics.get("champion_accuracy"), (int, float)):
        result["champion_accuracy_same_window"] = float(metrics["champion_accuracy"])
    for station, value in (metrics.get("by_station") or {}).items():
        result[f"validation_accuracy_station_{station}"] = float(value)
    for step, value in (metrics.get("by_horizon") or {}).items():
        result[f"validation_accuracy_h{int(step) * 15}min"] = float(value)
    return result


def sync_models(conn, client: MlflowClient) -> int:
    exp = experiment(client, "pulso-transmi-models")
    try:
        client.get_registered_model(REGISTERED_MODEL)
    except mlflow.exceptions.MlflowException:
        client.create_registered_model(REGISTERED_MODEL, description="Perfil log-lineal + corrección de nivel reciente (Pulso TransMi)")
    rows = conn.execute(
        """
        select v.model_version, v.status, v.git_commit, v.trained_at, v.training_data_end, v.hyperparameters,
               v.validation_metrics, v.feature_set, a.artifact, a.sha256
        from pulso.model_versions v join pulso.model_artifacts a using (model_version)
        order by v.trained_at
        """
    ).fetchall()
    created = 0
    for version, status, commit, trained_at, data_end, hyper, metrics, features, blob, digest in rows:
        run = find_run(client, exp, "model_version", version)
        if run is None:
            with mlflow.start_run(experiment_id=exp, run_name=version) as active:
                mlflow.set_tags({"model_version": version, "git_commit": commit, "model_sha256": digest,
                                 "source": "supabase:pulso.model_versions"})
                mlflow.log_params({**{k: str(v) for k, v in (hyper or {}).items()},
                                   "training_data_end": data_end.isoformat(), "trained_at": trained_at.isoformat(),
                                   "features": " | ".join((features or {}).get("features", []))})
                mlflow.log_metrics(flat_metrics(metrics or {}))
                with tempfile.TemporaryDirectory() as tmp:
                    (Path(tmp) / "model.joblib").write_bytes(blob)
                    (Path(tmp) / "validation_metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False))
                    mlflow.log_artifacts(tmp, artifact_path="model")
                run_id = active.info.run_id
            mv = client.create_model_version(REGISTERED_MODEL, source=f"runs:/{run_id}/model", run_id=run_id,
                                             tags={"model_version": version})
            conn.execute(
                "update pulso.model_versions set mlflow_run_id = %s, mlflow_model_uri = %s where model_version = %s",
                (run_id, f"models:/{REGISTERED_MODEL}/{mv.version}", version),
            )
            created += 1
            run = client.get_run(run_id)
        client.set_tag(run.info.run_id, "status", status)
        if status == "active":
            for mv in client.search_model_versions(f"name = '{REGISTERED_MODEL}'"):
                if mv.run_id == run.info.run_id:
                    client.set_registered_model_alias(REGISTERED_MODEL, "champion", mv.version)
    return created


def sync_decisions(conn, client: MlflowClient) -> int:
    exp = experiment(client, "pulso-transmi-retraining")
    created = 0
    for decision_id, evaluated_at, trigger, decision, reason, active, candidate, signals in conn.execute(
        """select decision_id, evaluated_at, trigger, decision, reason, active_model_version,
                  candidate_model_version, signals from pulso.retraining_decisions order by decision_id"""
    ).fetchall():
        if find_run(client, exp, "decision_id", str(decision_id)):
            continue
        with mlflow.start_run(experiment_id=exp, run_name=f"{decision_id:04d}-{trigger}-{decision}"):
            mlflow.set_tags({"decision_id": str(decision_id), "trigger": trigger, "decision": decision,
                             "reason": reason, "active_model_version": active or "", "candidate_model_version": candidate or "",
                             "evaluated_at": evaluated_at.isoformat()})
            metrics = {f"candidate_{name}": float(value) for name, value in (signals.get("candidates") or {}).items()}
            if signals.get("champion_accuracy") is not None:
                metrics["champion_accuracy"] = float(signals["champion_accuracy"])
            mlflow.log_metrics(metrics)
            mlflow.log_dict(signals, "signals.json")
        created += 1
    return created


def sync_monitoring(conn, client: MlflowClient) -> int:
    exp = experiment(client, "pulso-transmi-monitoring")
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
    drift_names = ("drift_bias_alert_stations", "drift_psi_alert_stations", "drift_shape_alert_stations",
                   "drift_bias_max_abs", "drift_psi_max", "drift_shape_max")
    logged = 0
    runs: dict[str, Any] = {}
    for snapshot_id, cutoff, version, accuracy, submitted, expected, n, by_horizon, board in rows:
        key = version or "sin-modelo"
        if key not in runs:
            runs[key] = find_run(client, exp, "model_version", key) or client.create_run(
                exp, run_name=f"live-{key}", tags={"model_version": key})
        run = runs[key]
        last = int(run.data.tags.get("last_snapshot_id", 0)) if hasattr(run, "data") else 0
        if snapshot_id <= last:
            continue
        ts = int(cutoff.timestamp() * 1000)
        values = {"coverage_window": submitted / expected if expected else 0.0, "n_predictions": float(n)}
        if accuracy is not None:
            values["live_accuracy_rolling"] = float(accuracy)
        for step, value in (by_horizon or {}).items():
            values[f"live_accuracy_h{int(step) * 15}min"] = float(value)
        for window in ("cumulative", "rolling_24h"):
            row = (board or {}).get(window) or {}
            for field in ("accuracy", "coverage", "rank"):
                if isinstance(row.get(field), (int, float)):
                    values[f"leaderboard_{window}_{field}"] = float(row[field])
        for name, value in zip(drift_names, drift.get(cutoff, ())):
            if value is not None:
                values[name] = float(value)
        for name, value in values.items():
            client.log_metric(run.info.run_id, name, value, timestamp=ts, step=snapshot_id)
        client.set_tag(run.info.run_id, "last_snapshot_id", str(snapshot_id))
        runs[key] = client.get_run(run.info.run_id)
        logged += 1
    return logged


def main() -> None:
    mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", f"sqlite:///{ROOT / 'mlflow.db'}"))
    client = MlflowClient()
    with connect() as conn:
        print(f"modelos nuevos: {sync_models(conn, client)}")
        print(f"decisiones nuevas: {sync_decisions(conn, client)}")
        print(f"snapshots de monitoreo: {sync_monitoring(conn, client)}")


if __name__ == "__main__":
    main()
