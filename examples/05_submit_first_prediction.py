"""Envía la primera predicción auditada del modelo versionado en MLflow."""

from __future__ import annotations

from pathlib import Path

from pulso_transmi.submission import submit


MODEL_DIR = Path("artifacts/models/log-linear-profile-20260918T222118Z-7fd6363")


if __name__ == "__main__":
    result = submit(MODEL_DIR)
    print(f"Envío aceptado: {result['client_run_id']} ({result['predictions']} predicciones)")
