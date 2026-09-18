"""Entrena el primer modelo, lo empaqueta en joblib y lo registra en MLflow.

Ejecuta desde la raíz:
    python examples/04_train_and_log_model.py

Por defecto usa el tracking local ``sqlite:///mlflow.db``. Define
``MLFLOW_TRACKING_URI`` si se debe registrar en un servidor compartido.
"""

from __future__ import annotations

from pulso_transmi import PulsoTransmiClient
from pulso_transmi.training import train_and_log


def main() -> None:
    with PulsoTransmiClient() as client:
        meta = client.meta()
        observations = client.observations_dataframe()
        context = client.context_dataframe()
    data = observations.merge(context, on="observed_at", how="inner", validate="many_to_one")
    result = train_and_log(data, meta)
    print(f"Modelo: {result.model_version}")
    print(f"Accuracy de validación: {result.accuracy:.2f}")
    print(f"Joblib: {result.model_path}")
    print(f"MLflow run_id: {result.run_id}")


if __name__ == "__main__":
    main()
