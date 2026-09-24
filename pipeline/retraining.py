"""Etapa 4 · Reentrenamiento: decidir → entrenar candidatos → validar → promover.

Disparadores (se registran en ``retraining_decisions``):
- ``initial``: no hay campeón activo;
- ``scheduled``: el campeón tiene más de ``every_hours`` de datos sin ver;
- ``drift``: la etapa de desempeño levantó alertas (con cooldown).

Validación sin fuga: la ventana de evaluación son las últimas ``eval_hours``
virtuales. Cada receta candidata se entrena SOLO con datos anteriores a la
ventana y se evalúa con ciclos simulados idénticos a los reales. El campeón se
evalúa igual (tal cual, si su entrenamiento terminó antes de la ventana; si no,
re-ajustando su receta con los mismos datos). Solo se promueve si el mejor
candidato supera al campeón por ``min_gain`` puntos; entonces se re-entrena esa
receta con todos los datos y se guarda como nueva versión activa.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from common import Skip, connect, git_commit, latest_observed_at, load_active, load_config, new_version_name, promote, register_model, stage, training_frame
from pulso_transmi.forecasting import AdaptiveProfileForecaster, ForecasterConfig, cycle_cutoffs, official_accuracy, simulate_cycles


def recipes(config: dict[str, Any]) -> dict[str, ForecasterConfig]:
    base = {key: value for key, value in config["model"].items()}
    result = {}
    for candidate in config["retraining"].get("candidates", []):
        overrides = {key: value for key, value in candidate.items() if key != "name"}
        values = {**base, "train_half_life_days": None, "train_window_days": None, **overrides}
        result[candidate["name"]] = ForecasterConfig.from_mapping(values)
    return result or {"default": ForecasterConfig.from_mapping(base)}


def evaluate(forecaster: AdaptiveProfileForecaster, data: pd.DataFrame, window_start: pd.Timestamp) -> dict[str, Any]:
    recent = data[data["observed_at"] > window_start - pd.Timedelta(hours=2)]
    return official_accuracy(simulate_cycles(forecaster, recent, cycle_cutoffs(recent, window_start)))


def trigger_for(conn: psycopg.Connection, cfg: dict[str, Any], champion, cutoff: pd.Timestamp, drift: bool, force: bool) -> str | None:
    if champion is None:
        return "initial"
    if force:
        return "manual"
    # Horas virtuales desde lo último que se evaluó (entrenamiento del campeón o
    # última decisión): evita re-evaluar en cada ciclo una vez vencido el plazo.
    last_eval = conn.execute("select max((signals->>'cutoff')::timestamptz) from pulso.retraining_decisions").fetchone()[0]
    reference = max(champion.training_data_end, pd.Timestamp(last_eval) if last_eval else champion.training_data_end)
    elapsed = cutoff - reference
    if elapsed >= pd.Timedelta(hours=cfg["every_hours"]):
        return "scheduled"
    if drift and elapsed >= pd.Timedelta(hours=cfg["drift_cooldown_hours"]):
        return "drift"
    return None


def decide(conn: psycopg.Connection, *, trigger: str, decision: str, reason: str, active: str | None,
           candidate: str | None, signals: dict[str, Any]) -> None:
    conn.execute(
        """insert into pulso.retraining_decisions
           (trigger, decision, reason, active_model_version, candidate_model_version, signals, git_commit, github_run_id)
           values (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (trigger, decision, reason, active, candidate, Jsonb(signals), git_commit(), os.getenv("GITHUB_RUN_ID")),
    )


def run(conn: psycopg.Connection, *, drift: bool = False, force: bool = False, drift_alerts: list[str] | None = None) -> dict[str, Any]:
    config = load_config()
    cfg = config["retraining"]
    with stage(conn, "retraining") as details:
        cutoff = latest_observed_at(conn)
        if cutoff is None:
            raise Skip("no hay observaciones")
        champion = load_active(conn)
        trigger = trigger_for(conn, cfg, champion, cutoff, drift, force)
        if trigger is None:
            raise Skip("sin disparador: campeón vigente y sin drift")
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

        champion_acc = None
        if champion is not None:
            reference = champion.forecaster
            if champion.training_data_end > window_start:
                reference = AdaptiveProfileForecaster(champion.forecaster.config).fit(before_window)
            champion_acc = evaluate(reference, data, window_start)["accuracy"]
            details["champion"] = {"version": champion.version, "accuracy": None if champion_acc is None else round(champion_acc, 3)}

        signals = {"cutoff": cutoff.isoformat(), "eval_window_start": window_start.isoformat(),
                   "candidates": details["candidates"], "champion_accuracy": champion_acc, "drift_alerts": drift_alerts or []}
        promote_it = champion is None or (best_acc >= cfg["min_accuracy"] and best_acc >= (champion_acc or 0.0) + cfg["min_gain"])
        if not promote_it:
            reason = (f"se conserva {champion.version}: mejor candidato {best} {best_acc:.2f} "
                      f"no supera {champion_acc:.2f} + {cfg['min_gain']}")
            decide(conn, trigger=trigger, decision="keep", reason=reason, active=champion.version, candidate=None, signals=signals)
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
        decide(conn, trigger=trigger, decision="promote", reason=reason,
               active=champion.version if champion else None, candidate=version, signals=signals)
        details.update({"decision": "promote", "model_version": version, "accuracy": round(best_acc, 3)})
    return details


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reentrenamiento con validación campeón vs. candidato")
    parser.add_argument("--force", action="store_true", help="evalúa candidatos aunque no haya disparador")
    args = parser.parse_args()
    with connect() as connection:
        print(run(connection, force=args.force))
