"""Entrenamiento, empaquetado y trazabilidad MLflow del primer modelo."""

from __future__ import annotations

import json
import os
import subprocess
from hashlib import sha256
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from pulso_transmi.baseline import LogLinearProfileModel, accuracy_by_station, temporal_split

# MLflow emits an agent-tracing hint at import time; this project uses regular
# experiment tracking, not tracing.
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

ROOT = Path(__file__).resolve().parents[2]
MODEL_NAME = "log-linear-profile"
EXPERIMENT_NAME = "pulso-transmi-models"


@dataclass(frozen=True)
class TrainingResult:
    model_version: str
    run_id: str
    model_path: Path
    package_path: Path
    accuracy: float
    accuracy_by_station: dict[str, float]
    train_rows: int
    validation_rows: int
    cutoff: str


def _git_state() -> dict[str, str]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
            ).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return "unknown"

    return {"git_commit": run("rev-parse", "HEAD"), "git_dirty": str(bool(run("status", "--porcelain")))}


def _require_mlflow() -> Any:
    try:
        import mlflow
        import mlflow.data
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError("Instala las dependencias ML con: python -m pip install -e '.[ml]'") from exc
    return mlflow


def _tracking_setup(mlflow: Any) -> None:
    tracking_uri = os.getenv("MLFLOW_TRACKING_URI", f"sqlite:///{ROOT / 'mlflow.db'}")
    mlflow.set_tracking_uri(tracking_uri)
    if mlflow.get_experiment_by_name(EXPERIMENT_NAME) is None:
        mlflow.create_experiment(EXPERIMENT_NAME, artifact_location=(ROOT / "mlartifacts").as_uri())
    mlflow.set_experiment(EXPERIMENT_NAME)


def _dataset_params(meta: dict[str, Any]) -> dict[str, str | int | float]:
    dataset = meta["dataset"]
    params: dict[str, str | int | float] = {
        "api_version": meta["api_version"],
        "dataset": dataset["dataset"],
        "history_start": dataset["history_start"],
        "history_end": dataset["history_end"],
        "frequency_minutes": dataset["frequency_minutes"],
        "station_count": dataset["station_count"],
    }
    params.update({f"sha256_{name.split('.')[0]}": item["sha256"] for name, item in dataset["files"].items()})
    return params


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def train_and_log(
    data: pd.DataFrame,
    meta: dict[str, Any],
    *,
    validation_days: int = 7,
    artifact_root: Path | None = None,
) -> TrainingResult:
    """Entrena, empaqueta con joblib y deja un run completo e inmutable en MLflow."""
    if data.empty:
        raise ValueError("no hay datos para entrenar")
    if artifact_root is None:
        artifact_root = ROOT / "artifacts" / "models"

    train, validation = temporal_split(data, validation_days=validation_days)
    model = LogLinearProfileModel().fit(train)
    validation["prediction"] = model.predict(validation)
    scores = accuracy_by_station(validation).sort_index()
    accuracy = float(scores.mean())
    git = _git_state()
    created_at = datetime.now(UTC)
    model_version = f"{MODEL_NAME}-{created_at:%Y%m%dT%H%M%SZ}-{git['git_commit'][:7]}"
    package_path = artifact_root / model_version
    package_path.mkdir(parents=True, exist_ok=False)
    model_path = package_path / "model.joblib"
    joblib.dump(model, model_path)

    cutoff = pd.to_datetime(train["observed_at"], utc=True).max().isoformat()
    metrics = {
        "metric": "mean_station_accuracy",
        "metric_definition": "WAPE por estación y promedio simple entre 12 estaciones",
        "accuracy": accuracy,
        "accuracy_by_station": {str(key): float(value) for key, value in scores.items()},
        "train_rows": len(train),
        "validation_rows": len(validation),
        "training_data_end": cutoff,
        "validation_start": pd.to_datetime(validation["observed_at"], utc=True).min().isoformat(),
        "validation_end": pd.to_datetime(validation["observed_at"], utc=True).max().isoformat(),
    }
    manifest = {
        "model_version": model_version,
        "created_at": created_at.isoformat(),
        "model_class": "pulso_transmi.baseline.LogLinearProfileModel",
        "target": "log(demand)",
        "prediction_transform": "exp(predicted_log_demand)",
        "features": ["station_id", "observed_at -> slot × weekend", "rain_forecast", "event_intensity"],
        "excluded_features": ["rain_mm", "temperature_c", "lags"],
        "model_sha256": _file_sha256(model_path),
        "validation": f"últimos {validation_days} días, split temporal",
        "dataset": meta["dataset"],
        "api_version": meta["api_version"],
        **git,
    }
    metrics_path = package_path / "metrics.json"
    manifest_path = package_path / "manifest.json"
    metrics_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n")
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    mlflow = _require_mlflow()
    _tracking_setup(mlflow)
    source_url = f"{os.getenv('PULSO_API_URL', 'https://pulso-transmi.72-60-245-2.sslip.io').rstrip('/')}/v1/downloads"
    with mlflow.start_run(run_name=model_version) as run:
        result = TrainingResult(
            model_version=model_version,
            run_id=run.info.run_id,
            model_path=model_path,
            package_path=package_path,
            accuracy=accuracy,
            accuracy_by_station=metrics["accuracy_by_station"],
            train_rows=len(train),
            validation_rows=len(validation),
            cutoff=cutoff,
        )
        (package_path / "training_result.json").write_text(
            json.dumps(asdict(result), default=str, indent=2) + "\n"
        )
        mlflow.set_tags(
            {
                "stage": "validation",
                "model_name": MODEL_NAME,
                "model_version": model_version,
                "metric_definition": metrics["metric_definition"],
                **git,
            }
        )
        mlflow.log_params(
            {
                **_dataset_params(meta),
                "validation_days": validation_days,
                "target": manifest["target"],
                "prediction_transform": manifest["prediction_transform"],
                "features": ", ".join(manifest["features"]),
                "excluded_features": ", ".join(manifest["excluded_features"]),
            }
        )
        mlflow.log_input(
            mlflow.data.from_pandas(
                data.loc[:, ["station_id", "observed_at", "demand"]],
                source=f"{source_url}/observations.csv",
                name=meta["dataset"]["dataset"],
                digest=meta["dataset"]["files"]["observations.csv"]["sha256"][:16],
            ),
            context="training_and_validation",
        )
        mlflow.log_metrics(
            {
                "validation_accuracy": accuracy,
                **{f"validation_accuracy_station_{station}": value for station, value in metrics["accuracy_by_station"].items()},
            }
        )
        mlflow.log_artifacts(str(package_path), artifact_path="model_package")
        for source in [
            ROOT / "src" / "pulso_transmi" / "baseline.py",
            ROOT / "src" / "pulso_transmi" / "training.py",
            ROOT / "examples" / "03_log_linear_baseline.py",
            ROOT / "examples" / "04_train_and_log_model.py",
            ROOT / "docs" / "first-model.md",
            ROOT / "README.md",
            ROOT / "pyproject.toml",
        ]:
            mlflow.log_artifact(str(source), artifact_path="source")
    return result
