"""Contrato de submissions de Pulso TransMi: construcción y validación del payload."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

SCHEMA_VERSION = "1.0"
MAX_VALUE = 100_000


def cycle_targets(cycle: dict[str, Any]) -> pd.DataFrame:
    """Targets del ciclo tal como los define la API (se conserva el texto exacto)."""
    targets = cycle.get("targets") or []
    if cycle.get("state") != "open" or not targets:
        raise ValueError("no hay un ciclo abierto con targets para enviar")
    expected = cycle.get("expected_predictions")
    if expected is not None and len(targets) != expected:
        raise ValueError(f"la API anunció {expected} predicciones pero entregó {len(targets)} targets")
    frame = pd.DataFrame(targets)
    frame["station_id"] = frame["station_id"].astype(str)
    if frame.duplicated(["station_id", "target_at"]).any():
        raise ValueError("el ciclo contiene targets duplicados")
    return frame


def build_payload(
    *,
    cycle: dict[str, Any],
    targets: pd.DataFrame,
    values: pd.Series,
    client_run_id: str,
    model: dict[str, Any],
) -> dict[str, Any]:
    """Arma el cuerpo del POST /v1/submissions y valida cada valor."""
    if len(values) != len(targets):
        raise ValueError("hay que predecir exactamente un valor por target")
    predictions = []
    for (station_id, target_at), value in zip(targets[["station_id", "target_at"]].itertuples(index=False), values, strict=True):
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= MAX_VALUE:
            raise ValueError(f"predicción inválida para {station_id} {target_at}: {value}")
        predictions.append({"station_id": str(station_id), "target_at": str(target_at), "value": round(value, 4)})
    return {
        "schema_version": SCHEMA_VERSION,
        "cycle_id": cycle["cycle_id"],
        "client_run_id": client_run_id,
        "data_cutoff": cycle["data_cutoff"],
        "model": model,
        "predictions": predictions,
    }
