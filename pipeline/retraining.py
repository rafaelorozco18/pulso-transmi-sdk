"""Etapa 4 · Reentrenamiento: medir → decidir → entrenar candidatos → validar → promover.

Corre en cada ciclo (≈ cada hora). Disparadores (se registran en
``retraining_decisions``):
- ``initial``: no hay campeón activo;
- ``performance``: la accuracy rolling 24 h de lo enviado (métrica oficial,
  etapa de desempeño) cayó bajo ``accuracy_below``;
- ``drift``: la etapa de desempeño levantó alertas de datos, concepto o
  desempeño;
- ``scheduled``: el campeón lleva ``every_hours`` sin evaluarse;
- ``manual``: ``--force``.
``cooldown_hours`` (1 = cada ciclo) evita evaluar dos veces el mismo corte.

Validación sin fuga: la ventana de evaluación son las últimas ``eval_hours``
virtuales. Cada receta candidata (memoria del perfil y ancla de nivel, ver
``config.toml``) se entrena SOLO con datos anteriores a la ventana y se evalúa
con ciclos simulados idénticos a los reales. El campeón se evalúa igual (tal
cual, si su entrenamiento terminó antes de la ventana; si no, re-ajustando su
receta con los mismos datos). Solo se promueve si el mejor candidato supera al
campeón por ``min_gain`` puntos; entonces se re-entrena esa receta con todos
los datos y se guarda como nueva versión activa. Cada decisión guarda el
detalle de todos los candidatos para que ``tracking.py`` la versione en MLflow.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from common import Skip, connect, git_commit, latest_observed_at, load_active, load_config, new_version_name, promote, register_model, stage, training_frame
from pulso_transmi.forecasting import AdaptiveProfileForecaster, ForecasterConfig, cycle_cutoffs, history_slots, official_accuracy, simulate_cycles


def recipes(config: dict[str, Any]) -> dict[str, ForecasterConfig]:
    base = {key: value for key, value in config["model"].items()}
    result = {}
    for candidate in config["retraining"].get("candidates", []):
        overrides = {key: value for key, value in candidate.items() if key != "name"}
        values = {**base, "train_half_life_days": None, "train_window_days": None, **overrides}
        result[candidate["name"]] = ForecasterConfig.from_mapping(values)
    return result or {"default": ForecasterConfig.from_mapping(base)}


def evaluate(forecaster: AdaptiveProfileForecaster, data: pd.DataFrame, window_start: pd.Timestamp) -> dict[str, Any]:
    # Historia previa a la ventana suficiente para la corrección reciente y el ancla.
    margin = pd.Timedelta(minutes=15 * history_slots(forecaster.config)) + pd.Timedelta(hours=2)
    recent = data[data["observed_at"] > window_start - margin]
    return official_accuracy(simulate_cycles(forecaster, recent, cycle_cutoffs(recent, window_start)))


def choose_trigger(cfg: dict[str, Any], *, has_champion: bool, force: bool, elapsed: pd.Timedelta | None,
                   accuracy: float | None, drift: bool) -> str | None:
    """Regla de disparo pura (testeable). ``elapsed``: tiempo virtual desde la última evaluación."""
    if not has_champion:
        return "initial"
    if force:
        return "manual"
    if elapsed is not None and elapsed < pd.Timedelta(hours=cfg["cooldown_hours"]):
        return None
    if accuracy is not None and accuracy < cfg["accuracy_below"]:
        return "performance"
    if drift:
        return "drift"
    if elapsed is None or elapsed >= pd.Timedelta(hours=cfg["every_hours"]):
        return "scheduled"
    return None


def last_evaluation(conn: psycopg.Connection, champion) -> pd.Timestamp:
    """Corte de la última evaluación (decisión registrada o fin de entrenamiento del campeón)."""
    last = conn.execute("select max((signals->>'cutoff')::timestamptz) from pulso.retraining_decisions").fetchone()[0]
    return max(champion.training_data_end, pd.Timestamp(last)) if last else champion.training_data_end


def decide(conn: psycopg.Connection, *, trigger: str, decision: str, reason: str, active: str | None,
           candidate: str | None, signals: dict[str, Any]) -> int:
    return conn.execute(
        """insert into pulso.retraining_decisions
           (trigger, decision, reason, active_model_version, candidate_model_version, signals, git_commit, github_run_id)
           values (%s, %s, %s, %s, %s, %s, %s, %s) returning decision_id""",
        (trigger, decision, reason, active, candidate, Jsonb(signals), git_commit(), os.getenv("GITHUB_RUN_ID")),
    ).fetchone()[0]


def run(conn: psycopg.Connection, *, drift: bool = False, accuracy: float | None = None, force: bool = False,
        drift_alerts: list[str] | None = None) -> dict[str, Any]:
    config = load_config()
    cfg = config["retraining"]
    with stage(conn, "retraining") as details:
        cutoff = latest_observed_at(conn)
        if cutoff is None:
            raise Skip("no hay observaciones")
        champion = load_active(conn)
        elapsed = cutoff - last_evaluation(conn, champion) if champion else None
        trigger = choose_trigger(cfg, has_champion=champion is not None, force=force, elapsed=elapsed, accuracy=accuracy, drift=drift)
        if trigger is None:
            if elapsed is not None and elapsed < pd.Timedelta(hours=cfg["cooldown_hours"]):
                raise Skip("este corte ya fue evaluado")
            raise Skip(f"sin disparador: accuracy {accuracy:.2f} ≥ {cfg['accuracy_below']} y sin drift" if accuracy is not None
                       else "sin disparador: campeón vigente y sin drift")
        details["trigger"] = trigger

        data = training_frame(conn, cutoff)
        window_start = cutoff - pd.Timedelta(hours=cfg["eval_hours"])
        before_window = data[data["observed_at"] <= window_start]
        options = recipes(config)

        scores: dict[str, dict[str, Any]] = {}
        for name, recipe in options.items():
            scores[name] = evaluate(AdaptiveProfileForecaster(recipe).fit(before_window), data, window_start)
        best = max(scores, key=lambda name: scores[name]["accuracy"] or 0.0)
        best_acc = scores[best]["accuracy"] or 0.0
        details["candidates"] = {name: round(score["accuracy"] or 0.0, 3) for name, score in scores.items()}

        champion_acc, champion_score = None, None
        if champion is not None:
            reference = champion.forecaster
            if champion.training_data_end > window_start:
                reference = AdaptiveProfileForecaster(champion.forecaster.config).fit(before_window)
            champion_score = evaluate(reference, data, window_start)
            champion_acc = champion_score["accuracy"]
            details["champion"] = {"version": champion.version, "accuracy": None if champion_acc is None else round(champion_acc, 3)}

        signals = {
            "cutoff": cutoff.isoformat(), "eval_window_start": window_start.isoformat(),
            "candidates": details["candidates"], "champion_accuracy": champion_acc,
            "live_accuracy": accuracy, "accuracy_below": cfg["accuracy_below"], "drift_alerts": drift_alerts or [],
            "best_candidate": best, "min_gain": cfg["min_gain"],
            "candidate_details": {
                name: {"config": options[name].as_dict(), "n": score["n"],
                       "by_horizon": score["by_horizon"], "by_station": score["by_station"]}
                for name, score in scores.items()
            },
            "champion_details": None if champion_score is None else {
                "version": champion.version, "config": champion.forecaster.config.__dict__,
                "by_horizon": champion_score["by_horizon"], "by_station": champion_score["by_station"],
            },
        }
        promote_it = champion is None or (best_acc >= cfg["min_accuracy"] and best_acc >= (champion_acc or 0.0) + cfg["min_gain"])
        if not promote_it:
            reason = (f"se conserva {champion.version}: mejor candidato {best} {best_acc:.2f} "
                      f"no supera {champion_acc:.2f} + {cfg['min_gain']}")
            details["decision_id"] = decide(conn, trigger=trigger, decision="keep", reason=reason, active=champion.version,
                                            candidate=None, signals=signals)
            details["decision"] = "keep"
            return details

        final = AdaptiveProfileForecaster(options[best]).fit(data)
        version = new_version_name(best)
        metrics = {"metric": "mean_station_accuracy · ciclos simulados", "recipe": best, **scores[best],
                   "eval_window_start": window_start.isoformat(), "eval_window_end": cutoff.isoformat(),
                   "champion_accuracy": champion_acc, "candidates": details["candidates"]}
        register_model(conn, version=version, forecaster=final, validation_metrics=metrics, status="candidate")
        promote(conn, version)
        reason = (f"{best} {best_acc:.2f} vs campeón {champion_acc:.2f}" if champion_acc is not None else f"modelo inicial {best} {best_acc:.2f}")
        details["decision_id"] = decide(conn, trigger=trigger, decision="promote", reason=reason,
                                        active=champion.version if champion else None, candidate=version, signals=signals)
        details.update({"decision": "promote", "model_version": version, "accuracy": round(best_acc, 3)})
    return details


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reentrenamiento con validación campeón vs. candidato")
    parser.add_argument("--force", action="store_true", help="evalúa candidatos aunque no haya disparador")
    args = parser.parse_args()
    with connect() as connection:
        print(run(connection, force=args.force))
