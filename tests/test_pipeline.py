import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("psycopg")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from collector import validate  # noqa: E402
from common import load_config  # noqa: E402
from drift import data_drift_signals, psi, shape_distance  # noqa: E402
from retraining import recipes  # noqa: E402


def test_collector_accepts_a_clean_batch() -> None:
    batch = pd.DataFrame({
        "station_id": ["02300", "10009"],
        "observed_at": ["2026-09-11T17:00:00Z", "2026-09-11T17:00:00Z"],
        "demand": [141, 91],
    })
    assert validate(batch, {"02300", "10009"}) == {"rows": 2, "slots_incompletos": 0}


@pytest.mark.parametrize(
    "change",
    [
        {"demand": [-1, 91]},
        {"observed_at": ["2026-09-11T17:07:00Z", "2026-09-11T17:00:00Z"]},
        {"station_id": ["99999", "10009"]},
        {"station_id": ["02300", "02300"]},
    ],
)
def test_collector_rejects_invalid_batches(change: dict) -> None:
    batch = pd.DataFrame({
        "station_id": ["02300", "10009"],
        "observed_at": ["2026-09-11T17:00:00Z", "2026-09-11T17:00:00Z"],
        "demand": [141, 91],
    }).assign(**change)
    with pytest.raises(ValueError):
        validate(batch, {"02300", "10009"})


def test_config_defines_competing_recipes() -> None:
    options = recipes(load_config())
    assert set(options) == {"hl14", "hl5", "win7"}
    assert options["win7"].train_window_days == 7 and options["win7"].train_half_life_days is None
    assert options["hl14"].lookback_slots == load_config()["model"]["lookback_slots"]


def test_psi_is_near_zero_for_the_same_distribution_and_large_for_a_shift() -> None:
    rng = np.random.default_rng(7)
    reference = rng.normal(5, 0.3, 4000)
    assert psi(reference, rng.normal(5, 0.3, 400))["psi"] < 0.1
    assert psi(reference, rng.normal(5.3, 0.3, 400))["psi"] > 0.25


def test_shape_distance_detects_a_moved_peak() -> None:
    hour = pd.Series([7, 8, 17, 18])
    expected = pd.Series([100.0, 50.0, 20.0, 10.0])
    assert shape_distance(expected * 1.3, expected, hour) == pytest.approx(0.0)
    assert shape_distance(expected[::-1].reset_index(drop=True), expected, hour) > 0.5


def _days(start: str, days: int, stations: dict[str, float], rng: np.random.Generator) -> pd.DataFrame:
    stamps = pd.date_range(start, periods=days * 96, freq="15min", tz="UTC")
    return pd.concat(
        pd.DataFrame({"station_id": sid, "observed_at": stamps, "demand": np.round(level * np.exp(rng.normal(0, 0.15, len(stamps))))})
        for sid, level in stations.items()
    ).reset_index(drop=True)


def test_data_drift_flags_only_the_station_whose_level_moved() -> None:
    rng = np.random.default_rng(3)
    cfg = load_config()["performance"]
    reference = _days("2026-09-01T05:00Z", 14, {"A": 200.0, "B": 200.0}, rng)
    current = _days("2026-09-15T05:00Z", 1, {"A": 200.0, "B": 280.0}, rng)
    flat = lambda station, observed_at: pd.Series(200.0, index=station.index)  # noqa: E731
    rows = {(sid, signal): alert for sid, signal, _, _, alert, _ in data_drift_signals(current, reference, flat, cfg)}
    assert rows[("B", "demand_psi")] and not rows[("A", "demand_psi")]
    assert not rows[("A", "profile_shape")] and not rows[("B", "profile_shape")]
