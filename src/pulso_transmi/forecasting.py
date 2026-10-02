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
3. Ancla de nivel (``anchor_slots`` > 0): la corrección no se desvanece hacia
   cero sino hacia el log-ratio medio de las últimas ``anchor_slots`` (p. ej.
   96 = 24 h). Ante drift de tendencia o de nivel sostenido, el perfil queda
   corto de forma persistente y el ancla conserva ese desplazamiento en todos
   los horizontes:
   ``log(pred / perfil) = ancla + (reciente − ancla) · decay ** h``.
4. Estacionalidad corta (``season_slots`` ≠ 0): si la demanda deja de seguir el
   ciclo diario y se repite con otro período (p. ej. cada 4 h = 16 slots), el
   pronóstico es el promedio de los últimos ``season_cycles`` períodos en el
   mismo punto del ciclo. Con ``season_slots = -1`` el período se detecta en
   cada corte: el que mejor reproduce los últimos ``SEASON_SELECT_SLOTS``
   slots, o el divisor más corto de ese período si ajusta casi igual (los
   múltiplos del período real empatan con él).

El backtest de ciclos simulados (``simulate_cycles``) reproduce exactamente
esa información disponible y es la misma función con la que el reentrenamiento
compara el campeón contra un candidato.
"""

from __future__ import annotations

import warnings
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from pulso_transmi.baseline import LogLinearProfileModel

SLOT = pd.Timedelta(minutes=15)
HORIZON_STEPS = (1, 2, 3, 4)
SEASON_PERIODS = range(8, 97)   # períodos candidatos al detectar (2 h a 24 h)
SEASON_SELECT_SLOTS = 16        # slots recientes con los que se elige el período
SEASON_TOLERANCE = 1.15         # error relativo para considerar empatados un período y sus múltiplos


@dataclass(frozen=True)
class ForecasterConfig:
    lookback_slots: int = 4
    decay: float = 0.8
    max_abs_log_ratio: float = 1.0
    train_half_life_days: float | None = None
    train_window_days: int | None = None
    anchor_slots: int = 0
    season_slots: int = 0       # 0 = perfil diario; -1 = detectar período; > 0 = período fijo
    season_cycles: int = 3      # períodos promediados en el modo estacional

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

    def level_correction(self, history: pd.DataFrame, data_cutoff: pd.Timestamp, slots: int | None = None) -> pd.Series:
        """Log-ratio medio real/perfil por estación en los últimos ``slots`` (≤ corte)."""
        slots = self.config.lookback_slots if slots is None else slots
        cutoff = pd.Timestamp(data_cutoff)
        recent = history.loc[pd.to_datetime(history["observed_at"], utc=True) <= cutoff].copy()
        recent["observed_at"] = pd.to_datetime(recent["observed_at"], utc=True)
        start = cutoff - SLOT * (slots - 1)
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
        stations = targets["station_id"].astype(str)
        recent = stations.map(self.level_correction(history, cutoff)).fillna(0.0).to_numpy()
        anchor_slots = anchor_of(self.config)
        anchor = stations.map(self.level_correction(history, cutoff, anchor_slots)).fillna(0.0).to_numpy() if anchor_slots else 0.0
        profile = (base * np.exp(anchor + (recent - anchor) * self.config.decay ** steps.to_numpy())).clip(lower=0.0)
        if not season_of(self.config):
            return profile
        frame = history.loc[:, ["station_id", "observed_at", "demand"]].copy()
        frame["station_id"] = frame["station_id"].astype(str)
        frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
        wide = frame.pivot_table(index="observed_at", columns="station_id", values="demand", aggfunc="last")
        seasonal = self.seasonal_cycle(wide, cutoff)
        values = [seasonal.at[step, station] if station in seasonal.columns else np.nan
                  for step, station in zip(steps.to_numpy(), stations, strict=True)]
        return pd.Series(np.where(np.isnan(values), profile, values), index=targets.index).clip(lower=0.0)

    def seasonal_cycle(self, wide: pd.DataFrame, data_cutoff: pd.Timestamp) -> pd.DataFrame:
        """Pronóstico estacional (horizonte × estación) con la demanda ≤ corte; NaN si falta historia."""
        cutoff = pd.Timestamp(data_cutoff)
        cycles = max(1, int(self.config.season_cycles))
        period = season_of(self.config)
        size = (max(SEASON_PERIODS) if period < 0 else period) * cycles + SEASON_SELECT_SLOTS
        grid = pd.date_range(end=cutoff, periods=size + 1, freq=SLOT)
        frame = wide.loc[:cutoff].reindex(grid).reindex(columns=self.stations)
        frame = frame.where(frame > 0)
        values = frame.to_numpy(dtype=float)
        last = len(grid) - 1
        if period < 0:
            period = detect_period(values, cycles)
        if period <= 0:
            return pd.DataFrame(np.nan, index=list(HORIZON_STEPS), columns=self.stations)
        rows = [seasonal_mean(values, last + step, period, cycles) for step in HORIZON_STEPS]
        return pd.DataFrame(rows, index=list(HORIZON_STEPS), columns=self.stations)


def seasonal_mean(values: np.ndarray, position: int, period: int, cycles: int) -> np.ndarray:
    """Promedio de la demanda ``k · period`` slots antes de ``position`` (k = 1..cycles)."""
    lags = [position - k * period for k in range(1, cycles + 1) if 0 <= position - k * period < len(values)]
    if not lags:
        return np.full(values.shape[1], np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # estación sin ningún dato en esos lags → NaN
        return np.nanmean(values[lags], axis=0)


def detect_period(values: np.ndarray, cycles: int) -> int:
    """Período (slots) que mejor reproduce los últimos slots; 0 si no hay historia suficiente."""
    last = len(values) - 1
    actual = values[last - SEASON_SELECT_SLOTS + 1: last + 1]
    errors = {}
    for period in SEASON_PERIODS:
        predicted = np.array([seasonal_mean(values, position, period, cycles)
                              for position in range(last - SEASON_SELECT_SLOTS + 1, last + 1)])
        valid = ~(np.isnan(actual) | np.isnan(predicted))
        if valid.mean() < 0.8:
            continue
        errors[period] = np.abs(actual - predicted)[valid].sum() / max(actual[valid].sum(), 1.0)
    if not errors:
        return 0
    best = min(errors, key=errors.get)
    # Los múltiplos del período real ajustan casi igual: se toma el divisor más corto que empate.
    return min(period for period, error in errors.items() if best % period == 0 and error <= errors[best] * SEASON_TOLERANCE)


def anchor_of(config: ForecasterConfig) -> int:
    """``anchor_slots`` con compatibilidad para modelos serializados antes de existir."""
    return int(getattr(config, "anchor_slots", 0) or 0)


def season_of(config: ForecasterConfig) -> int:
    """``season_slots`` con compatibilidad para modelos serializados antes de existir."""
    return int(getattr(config, "season_slots", 0) or 0)


def history_slots(config: ForecasterConfig) -> int:
    """Slots de historia que necesita la corrección de nivel (reciente y ancla) y la estacionalidad."""
    slots = max(config.lookback_slots, anchor_of(config))
    period = season_of(config)
    if period:
        cycles = max(1, int(getattr(config, "season_cycles", 3)))
        slots = max(slots, (max(SEASON_PERIODS) if period < 0 else period) * cycles + SEASON_SELECT_SLOTS)
    return slots


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
    anchor_slots = anchor_of(cfg)

    def mean_ratio(cutoff: pd.Timestamp, slots: int) -> pd.Series:
        window = log_ratio.loc[cutoff - SLOT * (slots - 1): cutoff]
        return window.mean().fillna(0.0).clip(-cfg.max_abs_log_ratio, cfg.max_abs_log_ratio)

    for cutoff in cutoffs:
        correction = mean_ratio(cutoff, cfg.lookback_slots)
        anchor = mean_ratio(cutoff, anchor_slots) if anchor_slots else 0.0
        seasonal = forecaster.seasonal_cycle(actual, cutoff) if season_of(cfg) else None
        for step in HORIZON_STEPS:
            target = cutoff + SLOT * step
            if target not in actual.index:
                continue
            prediction = (base.loc[target] * np.exp(anchor + (correction - anchor) * cfg.decay ** step)).clip(lower=0.0)
            if seasonal is not None:
                prediction = seasonal.loc[step].reindex(actual.columns).fillna(prediction)
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
