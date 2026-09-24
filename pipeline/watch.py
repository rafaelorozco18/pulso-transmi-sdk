"""Orquestador: vigila la API y encadena las cuatro etapas en cada ciclo nuevo.

    colector → inferencia (POST) → desempeño → reentrenamiento

Los ciclos se abren cada ~30 min y cierran ~25 min después; un cron horario
pierde la mayoría. Este vigilante sondea ``/v1/forecast-cycles/current`` cada
``poll_seconds`` durante ``watch_minutes`` y GitHub Actions lo relanza en
cadena. Cada etapa es idempotente, así que reinicios o ejecuciones solapadas
no duplican datos, predicciones ni submissions.

    python pipeline/watch.py            # vigila según config.toml
    python pipeline/watch.py --once     # una sola pasada (útil en local)
"""

from __future__ import annotations

import argparse
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime

import collector
import inference
import performance
import retraining
from common import Api, connect, load_config

COLLECT_EVERY_SECONDS = 600


@dataclass
class WatchState:
    seen: dict[str, str] = field(default_factory=dict)  # cycle_id -> estado final
    evaluated: set[str] = field(default_factory=set)
    last_collect: float = 0.0
    failures: int = 0


def safe(label: str, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        print(f"[{label}] ERROR\n{traceback.format_exc()}")
        return None


def tick(state: WatchState) -> None:
    with Api() as api:
        cycle = api.current_cycle()
        cycle_id = cycle.get("cycle_id") if cycle and cycle.get("state") == "open" else None
        pending = cycle_id is not None and state.seen.get(cycle_id) != "accepted"
        if not pending and time.monotonic() - state.last_collect < COLLECT_EVERY_SECONDS:
            return
        with connect() as conn:
            if safe("collector", collector.run, conn) is None:
                state.failures += 1
            state.last_collect = time.monotonic()
            if not pending:
                return

            result = safe("inference", inference.run, conn, api, cycle)
            if result is None:
                state.failures += 1
                state.seen.setdefault(cycle_id, "failed")
            elif result.get("status") == "accepted" or "ya tiene" in str(result.get("reason", "")):
                state.seen[cycle_id] = "accepted"
            else:
                state.seen[cycle_id] = result.get("reason", "skipped")

            if cycle_id in state.evaluated:
                return
            state.evaluated.add(cycle_id)
            perf = safe("performance", performance.run, conn, api) or {}
            outcome = safe("retraining", retraining.run, conn, drift=bool(perf.get("drift")), drift_alerts=perf.get("alerts"))
            if outcome is None:
                state.failures += 1


def summary(state: WatchState) -> str:
    accepted = sum(status == "accepted" for status in state.seen.values())
    lines = [
        "## Pulso TransMi · vigilante",
        f"- ciclos vistos: **{len(state.seen)}** · aceptados: **{accepted}** · fallas de etapa: **{state.failures}**",
        "",
        "| ciclo | estado |",
        "|---|---|",
        *[f"| `{cycle}` | {status} |" for cycle, status in state.seen.items()],
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--minutes", type=float, help="sobrescribe schedule.watch_minutes")
    args = parser.parse_args()
    cfg = load_config()["schedule"]
    deadline = time.monotonic() + 60 * (args.minutes if args.minutes is not None else cfg["watch_minutes"])
    state = WatchState()
    print(f"vigilante iniciado {datetime.now(UTC):%Y-%m-%d %H:%M:%SZ} · sondeo cada {cfg['poll_seconds']} s")
    while True:
        try:
            tick(state)
        except Exception:  # API o BD caídas: se reintenta en el próximo sondeo
            state.failures += 1
            print(f"[tick] ERROR\n{traceback.format_exc()}")
        if args.once or time.monotonic() >= deadline:
            break
        time.sleep(cfg["poll_seconds"])

    report = summary(state)
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as file:
            file.write(report + "\n")
    missed = [cycle for cycle, status in state.seen.items() if status != "accepted"]
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
