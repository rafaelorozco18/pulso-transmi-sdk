import pandas as pd
import pytest

from pulso_transmi.baseline import LogLinearProfileModel, accuracy_by_station, temporal_split


def test_accuracy_is_average_of_station_wapes() -> None:
    frame = pd.DataFrame(
        {
            "station_id": ["00001", "00001", "00002", "00002"],
            "demand": [100, 100, 10, 10],
            "prediction": [90, 90, 5, 5],
        }
    )
    scores = accuracy_by_station(frame)
    assert scores["00001"] == 90
    assert scores["00002"] == 50
    assert scores.mean() == 70


def test_temporal_split_keeps_future_out_of_training() -> None:
    frame = pd.DataFrame(
        {
            "observed_at": pd.date_range("2026-01-01", periods=9, freq="D", tz="UTC"),
            "station_id": ["00001"] * 9,
        }
    )
    train, validation = temporal_split(frame, validation_days=2)
    assert train["observed_at"].max() < validation["observed_at"].min()
    assert len(train) == 7
    assert len(validation) == 2


def test_model_uses_context_and_predicts_non_negative() -> None:
    pytest.importorskip("sklearn")
    timestamps = pd.date_range("2026-01-05", periods=8, freq="15min", tz="America/Bogota")
    train = pd.DataFrame(
        {
            "station_id": ["00001"] * len(timestamps),
            "observed_at": timestamps,
            "demand": [10, 12, 14, 16, 18, 20, 22, 24],
            "rain_forecast": [0.0] * len(timestamps),
            "event_intensity": [0.0] * len(timestamps),
        }
    )
    model = LogLinearProfileModel().fit(train)
    prediction = model.predict(train)
    assert len(prediction) == len(train)
    assert (prediction >= 0).all()
