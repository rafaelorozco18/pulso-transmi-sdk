"""Entrena y valida el primer modelo de demanda con un corte temporal de 7 días.

Ejecuta desde la raíz:
    python examples/03_log_linear_baseline.py
"""

from __future__ import annotations

from pulso_transmi import PulsoTransmiClient
from pulso_transmi.baseline import LogLinearProfileModel, accuracy_by_station, temporal_split


def main() -> None:
    with PulsoTransmiClient() as client:
        observations = client.observations_dataframe()
        context = client.context_dataframe()

    data = observations.merge(context, on="observed_at", how="inner", validate="many_to_one")
    train, validation = temporal_split(data, validation_days=7)
    model = LogLinearProfileModel().fit(train)
    validation["prediction"] = model.predict(validation)
    scores = accuracy_by_station(validation)

    print(f"Filas train: {len(train):,} | validación: {len(validation):,}")
    print("Accuracy oficial por estación (WAPE por estación; promedio simple):")
    print(scores.round(2).to_string())
    print(f"\nAccuracy promedio: {scores.mean():.2f}")


if __name__ == "__main__":
    main()
