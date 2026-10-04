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
from retraining import choose_trigger, recipes  # noqa: E402


def test_collector_accepts_a_clean_batch() -> None:
    batch = pd.DataFrame({
        "station_id": ["02300", "10009"],
        "observed_at": ["2026-09-11T17:00:00Z", "2026-09-11T17:00:00Z"],
        "demand": [141, 91],
    })
    clean, details = validate(batch, {"02300", "10009"})
    assert details == {"rows": 2, "slots_incompletos": 0} and clean["demand"].tolist() == [141, 91]


def test_collector_reads_schema_v2_measurements() -> None:
    # API 0.9.0 (3-oct): la demanda pasa a measurement = {value: "546.00", unit, quality}.
    batch = pd.DataFrame({
        "station_id": ["02300", "10009", "02300", "10009"],
        "observed_at": ["2026-09-20T12:00:00Z"] * 2 + ["2026-09-20T12:15:00Z"] * 2,
        "demand": [556, 360, None, None],
        "schema_version": [None, None, 2, 2],
        "measurement": [None, None,
                        {"value": "546.00", "unit": "passengers", "quality": "observed"},
                        {"value": None, "unit": "passengers", "quality": "missing"}],
    })
    clean, details = validate(batch, {"02300", "10009"}, pd.Timestamp("2026-09-20T12:00:00Z"))
    assert clean["demand"].tolist() == [556, 360, 546]
    assert details["faltantes_api"] == 1 and details["slots_incompletos"] == 1


def test_collector_fails_on_an_unknown_unit() -> None:
    batch = pd.DataFrame({
        "station_id": ["02300"], "observed_at": ["2026-09-20T12:15:00Z"], "demand": [None],
        "measurement": [{"value": "5.46", "unit": "hundreds", "quality": "observed"}],
    })
    with pytest.raises(ValueError, match="unidad"):
        validate(batch, {"02300"})


def test_collector_quarantines_a_few_bad_rows_instead_of_dropping_the_batch() -> None:
    times = [f"2026-09-20T{h:02d}:00:00Z" for h in range(10)]
    batch = pd.DataFrame({"station_id": "02300", "observed_at": times, "demand": [100] * 9 + [-5]})
    clean, details = validate(batch, {"02300"})
    assert len(clean) == 9 and details["descartadas"] == {"demanda_invalida": 1}


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
    assert {"hl14", "hl5", "win7", "hl14-a24", "hl5-a24", "hl5-a12"} <= set(options)
    assert options["win7"].train_window_days == 7 and options["win7"].train_half_life_days is None
    assert options["hl14"].lookback_slots == load_config()["model"]["lookback_slots"]
    assert options["hl14"].anchor_slots == 0 and options["hl14-a24"].anchor_slots == 96
    assert options["hl14"].season_slots == 0 and options["seas-k3"].season_slots == -1
    assert options["seas-k3"].season_cycles == 3
    assert options["trend-k4"].trend_slots == 4 and options["hl14"].trend_slots == 0


HOUR = pd.Timedelta(hours=1)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"has_champion": False}, "initial"),
        ({"force": True}, "manual"),
        ({"accuracy": 81.5}, "performance"),          # bajo el umbral mínimo → reentrena en este ciclo
        ({"accuracy": 84.0, "drift": True}, "drift"),
        ({"accuracy": 84.0}, None),                   # todo bien: no gasta el ciclo
        ({"accuracy": 84.0, "elapsed": 30 * HOUR}, "scheduled"),
        ({"accuracy": 81.5, "elapsed": 0 * HOUR}, None),  # ese corte ya se evaluó
    ],
)
def test_retraining_trigger_policy(kwargs: dict, expected: str | None) -> None:
    cfg = load_config()["retraining"]
    args = {"has_champion": True, "force": False, "elapsed": HOUR, "accuracy": None, "drift": False, **kwargs}
    assert choose_trigger(cfg, **args) == expected


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
