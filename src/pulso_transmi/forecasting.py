"""Pronóstico operativo por ciclo: perfil log-lineal + corrección por nivel reciente.

Cada ciclo de la competencia pide los cuatro slots siguientes (15-60 minutos)
al ``data_cutoff``. En ese momento la demanda observada hasta el corte ya es
pública, así que el pronóstico combina dos piezas:

1. ``LogLinearProfileModel``: perfil slot × tipo de día por estación. La API no
   publica contexto después del corte inicial, de modo que lluvia pronosticada
   e intensidad de evento se fijan en cero (condición neutra) para predecir.
2. Corrección de nivel: media del log-ratio ``real / perfil`` en los últimos
   ``lookback_slots`` slots de cada estación, amortiguada por ``decay ** h``.
   Absorbe eventos, lluvia y cambios de nivel (drift) que el perfil no ve.

El backtest de ciclos simulados (``simulate_cycles``) reproduce exactamente
esa información disponible y es la misma función con la que el reentrenamiento
compara el campeón contra un candidato.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from pulso_transmi.baseline import LogLinearProfileModel

SLOT = pd.Timedelta(minutes=15)
HORIZON_STEPS = (1, 2, 3, 4)


@dataclass(frozen=True)
class ForecasterConfig:
    lookback_slots: int = 4
    decay: float = 0.8
    max_abs_log_ratio: float = 1.0
    train_half_life_days: float | None = None
    train_window_days: int | None = None

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "ForecasterConfig":
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in values.items() if key in known})

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def neutral_context(frame: pd.DataFrame) -> pd.DataFrame:
    """Añade contexto neutro (sin lluvia ni evento) a filas sin contexto."""
    result = frame.copy()
    for column in ("rain_forecast", "event_intensity"):
        if column not in result:
            result[column] = 0.0
        result[column] = result[column].fillna(0.0).astype(float)
    return result


@dataclass
class AdaptiveProfileForecaster:
    """Modelo servible: perfil entrenado + hiperparámetros de corrección."""

    config: ForecasterConfig = field(default_factory=ForecasterConfig)
    base: LogLinearProfileModel = field(default_factory=LogLinearProfileModel)
    training_data_end: str | None = None

    def fit(self, training: pd.DataFrame) -> "AdaptiveProfileForecaster":
        """Entrena el perfil con observaciones y contexto (faltante → neutro)."""
        data = neutral_context(training)
        data["observed_at"] = pd.to_datetime(data["observed_at"], utc=True)
        data = data[data["demand"] > 0]
        end = data["observed_at"].max()
        if self.config.train_window_days:
            data = data[data["observed_at"] > end - pd.Timedelta(days=self.config.train_window_days)]
        weights = None
        if self.config.train_half_life_days:
            age_days = (end - data["observed_at"]).dt.total_seconds() / 86400
            weights = 0.5 ** (age_days / self.config.train_half_life_days)
        self.base = LogLinearProfileModel().fit(data.reset_index(drop=True), sample_weight=None if weights is None else weights.reset_index(drop=True))
        self.training_data_end = end.isoformat()
        return self

    @property
    def stations(self) -> list[str]:
        return sorted(self.base.models)

    def base_prediction(self, station_id: pd.Series, observed_at: pd.Series) -> pd.Series:
        frame = neutral_context(pd.DataFrame({"station_id": station_id.astype(str).to_numpy(), "observed_at": pd.to_datetime(observed_at, utc=True).to_numpy()}))
        return pd.Series(self.base.predict(frame).to_numpy(), index=station_id.index)

    def level_correction(self, history: pd.DataFrame, data_cutoff: pd.Timestamp) -> pd.Series:
        """Log-ratio medio real/perfil por estación en los últimos slots ≤ corte."""
        cutoff = pd.Timestamp(data_cutoff)
        recent = history.loc[pd.to_datetime(history["observed_at"], utc=True) <= cutoff].copy()
        recent["observed_at"] = pd.to_datetime(recent["observed_at"], utc=True)
        start = cutoff - SLOT * (self.config.lookback_slots - 1)
        recent = recent[(recent["observed_at"] >= start) & (recent["demand"] > 0)]
        if recent.empty:
            return pd.Series(0.0, index=pd.Index(self.stations, name="station_id"))
        recent["base"] = self.base_prediction(recent["station_id"], recent["observed_at"])
        log_ratio = np.log(recent["demand"].astype(float) / recent["base"])
        by_station = log_ratio.groupby(recent["station_id"].astype(str)).mean()
        limit = self.config.max_abs_log_ratio
        return by_station.clip(-limit, limit).reindex(self.stations, fill_value=0.0)

    def predict_cycle(self, history: pd.DataFrame, targets: pd.DataFrame, data_cutoff: str | pd.Timestamp) -> pd.Series:
        """Predice ``targets`` (station_id, target_at) con la historia hasta ``data_cutoff``."""
        cutoff = pd.Timestamp(data_cutoff)
        target_at = pd.to_datetime(targets["target_at"], utc=True)
        steps = ((target_at - cutoff) / SLOT).round().astype(int).clip(lower=1)
        base = self.base_prediction(targets["station_id"], target_at)
        correction = self.level_correction(history, cutoff)
        ratio = targets["station_id"].astype(str).map(correction).fillna(0.0).to_numpy()
        return (base * np.exp(ratio * self.config.decay ** steps.to_numpy())).clip(lower=0.0)


def cycle_cutoffs(observations: pd.DataFrame, start: pd.Timestamp, every: pd.Timedelta = pd.Timedelta(minutes=30)) -> list[pd.Timestamp]:
    """Cortes simulados: como los ciclos reales, cada 30 min y con 4 slots futuros observados."""
    timestamps = pd.to_datetime(observations["observed_at"], utc=True)
    last = timestamps.max() - SLOT * max(HORIZON_STEPS)
    first = pd.Timestamp(start).ceil(every)
    if first > last:
        return []
    return list(pd.date_range(first, last, freq=every))


def simulate_cycles(forecaster: AdaptiveProfileForecaster, observations: pd.DataFrame, cutoffs: list[pd.Timestamp]) -> pd.DataFrame:
    """Backtest sin fuga: en cada corte usa solo la demanda ≤ corte."""
    frame = observations.loc[:, ["station_id", "observed_at", "demand"]].copy()
    frame["station_id"] = frame["station_id"].astype(str)
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
    frame = frame[frame["station_id"].isin(forecaster.stations)]
    actual = frame.pivot_table(index="observed_at", columns="station_id", values="demand", aggfunc="last")
    long_index = actual.stack().index
    base_long = forecaster.base_prediction(pd.Series(long_index.get_level_values(1)), pd.Series(long_index.get_level_values(0)))
    base = pd.Series(base_long.to_numpy(), index=long_index).unstack()
    log_ratio = np.log(actual.where(actual > 0) / base)
    cfg = forecaster.config
    rows = []
    for cutoff in cutoffs:
        window = log_ratio.loc[cutoff - SLOT * (cfg.lookback_slots - 1): cutoff]
        correction = window.mean().fillna(0.0).clip(-cfg.max_abs_log_ratio, cfg.max_abs_log_ratio)
        for step in HORIZON_STEPS:
            target = cutoff + SLOT * step
            if target not in actual.index:
                continue
            prediction = base.loc[target] * np.exp(correction * cfg.decay ** step)
            rows.append(pd.DataFrame({
                "cutoff": cutoff, "target_at": target, "horizon_steps": step,
                "station_id": actual.columns, "demand": actual.loc[target].to_numpy(),
                "prediction": prediction.to_numpy(),
            }))
    if not rows:
        return pd.DataFrame(columns=["cutoff", "target_at", "horizon_steps", "station_id", "demand", "prediction"])
    return pd.concat(rows, ignore_index=True).dropna(subset=["demand"])


def official_accuracy(frame: pd.DataFrame, prediction_column: str = "prediction") -> dict[str, Any]:
    """Accuracy oficial (WAPE por estación, promedio simple) con desgloses."""
    if frame.empty:
        return {"accuracy": None, "n": 0, "by_station": {}, "by_horizon": {}}

    def acc(group: pd.DataFrame) -> float:
        total = group["demand"].sum()
        return float(100 * max(0.0, 1 - (group["demand"] - group[prediction_column]).abs().sum() / total)) if total > 0 else 0.0

    by_station = {str(key): acc(group) for key, group in frame.groupby("station_id")}
    by_horizon = {
        int(step): float(np.mean([acc(g) for _, g in group.groupby("station_id")]))
        for step, group in frame.groupby("horizon_steps")
    }
    return {
        "accuracy": float(np.mean(list(by_station.values()))),
        "n": int(len(frame)),
        "by_station": by_station,
        "by_horizon": by_horizon,
    }
