import sys
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("psycopg")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from collector import validate  # noqa: E402
from common import load_config  # noqa: E402
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
