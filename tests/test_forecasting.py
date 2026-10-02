import math

import numpy as np
import pandas as pd
import pytest

from pulso_transmi.forecasting import (
    AdaptiveProfileForecaster,
    ForecasterConfig,
    cycle_cutoffs,
    detect_period,
    official_accuracy,
    simulate_cycles,
)
from pulso_transmi.submission import build_payload, cycle_targets

pytest.importorskip("sklearn")

STATIONS = ["02300", "10009"]


def synthetic(days: int = 10, level_shift_from: pd.Timestamp | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    index = pd.date_range("2026-08-03", periods=days * 96, freq="15min", tz="UTC")
    rows = []
    for station, scale in zip(STATIONS, (200, 60), strict=True):
        local = index.tz_convert("America/Bogota")
        profile = scale * (1.2 + np.sin(2 * np.pi * (local.hour * 4 + local.minute // 15) / 96))
        demand = profile * np.exp(rng.normal(0, 0.1, len(index)))
        if level_shift_from is not None:
            demand = np.where(index >= level_shift_from, demand * 1.5, demand)
        rows.append(pd.DataFrame({"station_id": station, "observed_at": index, "demand": np.maximum(1, demand.round())}))
    return pd.concat(rows, ignore_index=True)


def test_serving_matches_backtest_for_the_same_cutoff() -> None:
    data = synthetic()
    model = AdaptiveProfileForecaster().fit(data[data["observed_at"] < "2026-08-11"])
    cutoff = pd.Timestamp("2026-08-12T10:00:00Z")
    simulated = simulate_cycles(model, data, [cutoff])
    targets = simulated[["station_id", "target_at"]].assign(target_at=lambda f: f["target_at"].map(lambda t: t.isoformat()))
    served = model.predict_cycle(data, targets, cutoff)
    np.testing.assert_allclose(served.to_numpy(), simulated["prediction"].to_numpy(), rtol=1e-9)


def test_serving_matches_backtest_with_a_level_anchor() -> None:
    data = synthetic(level_shift_from=pd.Timestamp("2026-08-11T00:00:00Z"))
    model = AdaptiveProfileForecaster(ForecasterConfig(anchor_slots=96)).fit(data[data["observed_at"] < "2026-08-11"])
    cutoff = pd.Timestamp("2026-08-12T10:00:00Z")
    simulated = simulate_cycles(model, data, [cutoff])
    targets = simulated[["station_id", "target_at"]].assign(target_at=lambda f: f["target_at"].map(lambda t: t.isoformat()))
    served = model.predict_cycle(data, targets, cutoff)
    np.testing.assert_allclose(served.to_numpy(), simulated["prediction"].to_numpy(), rtol=1e-9)


def short_cycle(days: int = 10, switch_at: str = "2026-08-10T00:00:00Z", period: int = 16) -> pd.DataFrame:
    """Ciclo diario hasta ``switch_at`` y luego un patrón que se repite cada ``period`` slots."""
    data = synthetic(days=days)
    position = (data["observed_at"] - pd.Timestamp("2026-08-03T00:00:00Z")) // pd.Timedelta(minutes=15)
    scale = data["station_id"].map({"02300": 200, "10009": 60})
    noise = np.exp(np.random.default_rng(3).normal(0, 0.05, len(data)))
    fast = (scale * (1.2 + np.sin(2 * np.pi * (position % period) / period)) * noise).round().clip(lower=1)
    return data.assign(demand=np.where(data["observed_at"] >= pd.Timestamp(switch_at), fast, data["demand"]))


def test_serving_matches_backtest_with_a_detected_season() -> None:
    data = short_cycle()
    model = AdaptiveProfileForecaster(ForecasterConfig(season_slots=-1, season_cycles=3)).fit(data[data["observed_at"] < "2026-08-10"])
    cutoff = pd.Timestamp("2026-08-12T10:00:00Z")
    simulated = simulate_cycles(model, data, [cutoff])
    targets = simulated[["station_id", "target_at"]].assign(target_at=lambda f: f["target_at"].map(lambda t: t.isoformat()))
    served = model.predict_cycle(data[data["observed_at"] <= cutoff], targets, cutoff)
    np.testing.assert_allclose(served.to_numpy(), simulated["prediction"].to_numpy(), rtol=1e-9)


def test_season_detects_a_short_period_the_daily_profile_cannot_follow() -> None:
    # Sabotaje del 18-sep: la demanda pasa a repetirse cada 4 h (16 slots).
    data = short_cycle(days=12)
    train = data[data["observed_at"] < "2026-08-10"]
    cutoffs = cycle_cutoffs(data, pd.Timestamp("2026-08-12T00:00:00Z"))
    profile = official_accuracy(simulate_cycles(AdaptiveProfileForecaster().fit(train), data, cutoffs))["accuracy"]
    seasonal_model = AdaptiveProfileForecaster(ForecasterConfig(season_slots=-1, season_cycles=3)).fit(train)
    seasonal = official_accuracy(simulate_cycles(seasonal_model, data, cutoffs))["accuracy"]
    assert seasonal > 90 and seasonal > profile + 15
    window = data[data["observed_at"] <= cutoffs[-1]].pivot_table(index="observed_at", columns="station_id", values="demand")
    grid = window.reindex(pd.date_range(end=cutoffs[-1], periods=300, freq="15min")).to_numpy(dtype=float)
    assert detect_period(grid, 3) == 16


def test_anchor_keeps_a_sustained_trend_at_long_horizons() -> None:
    # Tendencia sostenida (+1 %/h ≈ +27 % al día): la corrección sin ancla se
    # desvanece a 60 min; el ancla de 24 h conserva el desplazamiento.
    data = synthetic(days=12)
    start = pd.Timestamp("2026-08-11T00:00:00Z")
    hours = ((data["observed_at"] - start).dt.total_seconds() / 3600).clip(lower=0)
    data["demand"] = (data["demand"] * np.exp(0.01 * hours)).round()
    train = data[data["observed_at"] < start]
    cutoffs = cycle_cutoffs(data[data["observed_at"] >= start + pd.Timedelta(days=1)], start + pd.Timedelta(days=1))
    plain = AdaptiveProfileForecaster().fit(train)
    anchored = AdaptiveProfileForecaster(ForecasterConfig(anchor_slots=96)).fit(train)
    plain_acc = official_accuracy(simulate_cycles(plain, data, cutoffs))
    anchored_acc = official_accuracy(simulate_cycles(anchored, data, cutoffs))
    assert anchored_acc["by_horizon"][4] > plain_acc["by_horizon"][4] + 3


def test_backtest_does_not_use_data_after_the_cutoff() -> None:
    data = synthetic()
    model = AdaptiveProfileForecaster().fit(data[data["observed_at"] < "2026-08-11"])
    cutoff = pd.Timestamp("2026-08-12T10:00:00Z")
    tampered = data.copy()
    tampered.loc[tampered["observed_at"] > cutoff, "demand"] *= 10
    before = simulate_cycles(model, data, [cutoff])["prediction"].to_numpy()
    after = simulate_cycles(model, tampered, [cutoff])["prediction"].to_numpy()
    np.testing.assert_allclose(before, after)


def test_level_correction_recovers_a_shift_the_profile_does_not_know() -> None:
    shift = pd.Timestamp("2026-08-11T00:00:00Z")
    data = synthetic(level_shift_from=shift)
    train = data[data["observed_at"] < shift]
    cutoffs = cycle_cutoffs(data[data["observed_at"] >= shift], shift + pd.Timedelta(hours=2))
    static = AdaptiveProfileForecaster(ForecasterConfig(lookback_slots=1, decay=0.0)).fit(train)
    adaptive = AdaptiveProfileForecaster().fit(train)
    static_acc = official_accuracy(simulate_cycles(static, data, cutoffs))["accuracy"]
    adaptive_acc = official_accuracy(simulate_cycles(adaptive, data, cutoffs))["accuracy"]
    assert adaptive_acc > static_acc + 10


def test_cycle_cutoffs_leave_four_observed_horizons() -> None:
    data = synthetic(days=1)
    cutoffs = cycle_cutoffs(data, data["observed_at"].min())
    assert cutoffs[-1] + pd.Timedelta(minutes=60) <= data["observed_at"].max()
    assert all(c.minute in (0, 30) for c in cutoffs)


def cycle(targets: list[dict]) -> dict:
    return {"cycle_id": "cyc_x", "state": "open", "data_cutoff": "2026-09-11T17:00:00Z",
            "expected_predictions": len(targets), "targets": targets}


def test_payload_keeps_target_strings_and_rejects_invalid_values() -> None:
    raw = [{"station_id": "02300", "target_at": "2026-09-11T17:15:00Z", "horizon_minutes": 15}]
    targets = cycle_targets(cycle(raw))
    payload = build_payload(cycle=cycle(raw), targets=targets, values=pd.Series([12.345678]), client_run_id="run_x", model={})
    assert payload["predictions"] == [{"station_id": "02300", "target_at": "2026-09-11T17:15:00Z", "value": 12.3457}]
    with pytest.raises(ValueError):
        build_payload(cycle=cycle(raw), targets=targets, values=pd.Series([math.nan]), client_run_id="run_x", model={})


def test_cycle_targets_require_the_announced_count() -> None:
    bad = cycle([{"station_id": "02300", "target_at": "2026-09-11T17:15:00Z"}])
    bad["expected_predictions"] = 48
    with pytest.raises(ValueError):
        cycle_targets(bad)
