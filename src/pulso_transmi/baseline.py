"""Primer modelo reproducible para demanda de Pulso TransMi.

El modelo ajusta una regresión lineal sobre ``log(demand)`` por estación. Sus
features son el perfil ``slot × tipo_de_día`` y las dos señales conocidas al
momento de predecir: lluvia pronosticada e intensidad de evento. La
transformación inversa usa ``exp(predicción)`` (la mediana log-normal), que es
la elección adecuada para minimizar error absoluto y por tanto WAPE.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

TZ = "America/Bogota"
REQUIRED_FEATURE_COLUMNS = {"station_id", "observed_at", "rain_forecast", "event_intensity"}


def _require_sklearn() -> tuple[object, object, object, object]:
    try:
        from sklearn.compose import ColumnTransformer
        from sklearn.linear_model import LinearRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import OneHotEncoder
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError("Instala las dependencias ML con: python -m pip install -e '.[ml]'") from exc
    return ColumnTransformer, LinearRegression, Pipeline, OneHotEncoder


def model_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Deriva features que están disponibles para un horizonte de predicción."""
    missing = REQUIRED_FEATURE_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"faltan columnas para predecir: {sorted(missing)}")

    features = frame.loc[:, ["station_id", "observed_at", "rain_forecast", "event_intensity"]].copy()
    timestamps = pd.to_datetime(features["observed_at"], utc=True)
    local = timestamps.dt.tz_convert(TZ)
    features["slot_weekend"] = (
        (local.dt.hour * 4 + local.dt.minute // 15).astype(str)
        + "_"
        + (local.dt.dayofweek >= 5).astype(int).astype(str)
    )
    features["station_id"] = features["station_id"].astype("string")
    return features.drop(columns="observed_at")


@dataclass
class LogLinearProfileModel:
    """Modelo independiente por estación con perfil y contexto pronosticable."""

    models: dict[str, object] = field(default_factory=dict, init=False)

    def fit(self, frame: pd.DataFrame, sample_weight: pd.Series | None = None) -> "LogLinearProfileModel":
        if "demand" not in frame:
            raise ValueError("fit requiere la columna demand")
        if (frame["demand"] <= 0).any():
            raise ValueError("el modelo log-lineal requiere demand estrictamente positiva")

        ColumnTransformer, LinearRegression, Pipeline, OneHotEncoder = _require_sklearn()
        features = model_features(frame)
        target = np.log(frame["demand"].astype(float))
        self.models = {}
        for station_id, indices in features.groupby("station_id", observed=True).groups.items():
            preprocessor = ColumnTransformer(
                [
                    ("profile", OneHotEncoder(handle_unknown="ignore"), ["slot_weekend"]),
                    ("context", "passthrough", ["rain_forecast", "event_intensity"]),
                ],
                verbose_feature_names_out=False,
            )
            model = Pipeline(
                [("features", preprocessor), ("regression", LinearRegression())]
            )
            fit_params = {} if sample_weight is None else {"regression__sample_weight": sample_weight.loc[indices].to_numpy()}
            model.fit(features.loc[indices], target.loc[indices], **fit_params)
            self.models[str(station_id)] = model
        return self

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        if not self.models:
            raise RuntimeError("el modelo no ha sido entrenado")
        features = model_features(frame)
        predictions = pd.Series(index=frame.index, dtype=float)
        for station_id, indices in features.groupby("station_id", observed=True).groups.items():
            model = self.models.get(str(station_id))
            if model is None:
                raise ValueError(f"no hay modelo entrenado para station_id={station_id}")
            predictions.loc[indices] = np.exp(model.predict(features.loc[indices]))
        return predictions.clip(lower=0.0)


def temporal_split(frame: pd.DataFrame, validation_days: int = 7) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Divide un dataset por tiempo, sin mezclar observaciones futuras al train."""
    if validation_days < 1:
        raise ValueError("validation_days debe ser al menos 1")
    timestamps = pd.to_datetime(frame["observed_at"], utc=True)
    cutoff = timestamps.max() - pd.Timedelta(validation_days, unit="D")
    return frame.loc[timestamps <= cutoff].copy(), frame.loc[timestamps > cutoff].copy()


def accuracy_by_station(frame: pd.DataFrame, prediction_column: str = "prediction") -> pd.Series:
    """Accuracy oficial: WAPE por estación, seguido de promedio simple entre ellas."""
    required = {"station_id", "demand", prediction_column}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"faltan columnas para calcular accuracy: {sorted(missing)}")
    grouped = frame.groupby("station_id", observed=True)
    wape = grouped.apply(
        lambda group: (group["demand"] - group[prediction_column]).abs().sum() / group["demand"].sum(),
        include_groups=False,
    )
    return 100 * (1 - wape).clip(lower=0)
