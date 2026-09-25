"""Señales de data drift: ¿la demanda que llega se parece a la que vio el modelo?

Se comparan dos muestras por estación:

- **actual**: la demanda de la ventana rolling (24 h virtuales);
- **referencia**: la demanda de los últimos ``reference_days`` del entrenamiento
  del campeón, restringida a los mismos (slot, tipo de día) que la ventana
  actual. Así la mezcla de horas es la misma y un fin de semana o una
  madrugada no se confunden con drift.

Señales (una fila por estación en ``pulso.drift_signals``):

- ``demand_psi``: Population Stability Index de log(demanda) con deciles de la
  referencia. Convención: < 0,10 estable, 0,10–0,25 moderado, > 0,25 cambio
  mayor (alerta). ``details`` guarda el test KS de dos muestras y los
  histogramas para graficar la comparación.
- ``profile_shape``: distancia de variación total entre la participación de
  cada hora en la demanda real y en el perfil del campeón (0 = misma forma,
  1 = formas disjuntas). Detecta picos que se corren o se deforman aunque el
  total del día no cambie.
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

TZ = "America/Bogota"


def calendar_keys(observed_at: pd.Series) -> pd.DataFrame:
    """slot de 15 min, tipo de día y hora local de cada timestamp."""
    local = pd.to_datetime(observed_at, utc=True).dt.tz_convert(TZ)
    return pd.DataFrame({
        "slot": (local.dt.hour * 4 + local.dt.minute // 15).to_numpy(),
        "weekend": (local.dt.dayofweek >= 5).to_numpy(),
        "hour": local.dt.hour.to_numpy(),
    }, index=observed_at.index)


def psi(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> dict[str, Any]:
    """PSI con cortes en los cuantiles de la referencia (bins vacíos acotados a 1e-4)."""
    edges = np.unique(np.quantile(reference, np.linspace(0, 1, bins + 1)))
    edges[0], edges[-1] = -np.inf, np.inf
    ref_share = np.histogram(reference, edges)[0] / len(reference)
    cur_share = np.histogram(current, edges)[0] / len(current)
    ref_safe = np.clip(ref_share, 1e-4, None)
    cur_safe = np.clip(cur_share, 1e-4, None)
    value = float(np.sum((cur_safe - ref_safe) * np.log(cur_safe / ref_safe)))
    return {"psi": value, "reference_share": ref_share.round(4).tolist(), "current_share": cur_share.round(4).tolist()}


def shape_distance(demand: pd.Series, expected: pd.Series, hour: pd.Series) -> float:
    """Distancia de variación total entre la distribución horaria real y la esperada."""
    real = demand.groupby(hour).sum()
    base = expected.groupby(hour).sum().reindex(real.index)
    if real.sum() <= 0 or base.sum() <= 0:
        return 0.0
    return float(0.5 * (real / real.sum() - base / base.sum()).abs().sum())


def data_drift_signals(
    current: pd.DataFrame,
    reference: pd.DataFrame,
    base_prediction: Callable[[pd.Series, pd.Series], pd.Series],
    cfg: dict[str, Any],
) -> list[tuple[str, str, float, float, bool, dict[str, Any]]]:
    """Filas ``(station_id, signal, value, threshold, is_alert, details)`` por estación.

    ``current`` y ``reference`` traen station_id, observed_at y demand.
    """
    rows: list[tuple[str, str, float, float, bool, dict[str, Any]]] = []
    current = current[current["demand"] > 0].copy()
    reference = reference[reference["demand"] > 0].copy()
    if current.empty or reference.empty:
        return rows
    current = current.join(calendar_keys(current["observed_at"]))
    reference = reference.join(calendar_keys(reference["observed_at"]))
    bins = int(cfg.get("psi_bins", 10))
    for station_id, now in current.groupby("station_id"):
        keys = set(zip(now["slot"], now["weekend"]))
        ref = reference[reference["station_id"] == station_id]
        ref = ref[[key in keys for key in zip(ref["slot"], ref["weekend"])]]
        if len(ref) < bins * 5 or len(now) < bins * 2:
            continue
        log_ref = np.log(ref["demand"].astype(float).to_numpy())
        log_now = np.log(now["demand"].astype(float).to_numpy())
        stats = psi(log_ref, log_now, bins)
        ks = ks_2samp(log_ref, log_now)
        rows.append((
            str(station_id), "demand_psi", stats["psi"], cfg["psi_alert"], stats["psi"] > cfg["psi_alert"],
            {
                "ks_statistic": float(ks.statistic), "ks_pvalue": float(ks.pvalue),
                "n_current": len(now), "n_reference": len(ref),
                "median_ratio": float(np.exp(np.median(log_now) - np.median(log_ref))),
                "reference_share": stats["reference_share"], "current_share": stats["current_share"],
            },
        ))
        expected = base_prediction(now["station_id"], now["observed_at"])
        distance = shape_distance(now["demand"].astype(float), expected, now["hour"])
        rows.append((
            str(station_id), "profile_shape", distance, cfg["shape_alert"], distance > cfg["shape_alert"],
            {"hours": int(now["hour"].nunique())},
        ))
    return rows
