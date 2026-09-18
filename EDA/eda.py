"""EDA profundo de Pulso TransMi.

Descarga el corte publicado por la API (con verificación SHA-256 del SDK),
valida integridad, cuantifica los factores que explican la demanda, genera las
figuras del reporte y registra todo en MLflow como un run versionado.

Uso:
    python EDA/eda.py               # usa EDA/data si ya existe
    python EDA/eda.py --refresh     # vuelve a descargar desde la API
    python EDA/eda.py --no-mlflow   # no registra el run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates
import matplotlib.pyplot as plt
import matplotlib.ticker
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from scipy import stats
from scipy.cluster.hierarchy import dendrogram, fcluster, linkage
from scipy.optimize import curve_fit
from scipy.signal import find_peaks
from sklearn.metrics import adjusted_rand_score
from statsmodels.tsa.stattools import acf

from pulso_transmi import PulsoTransmiClient, PulsoTransmiError

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

EDA_DIR = Path(__file__).resolve().parent
REPO_DIR = EDA_DIR.parent
DATA_DIR = EDA_DIR / "data"
REPORT_DIR = EDA_DIR / "report"
FIG_DIR = REPORT_DIR / "figures"
OUT_DIR = EDA_DIR / "outputs"

TZ = "America/Bogota"
FILES = ["stations.csv", "observations.csv", "context.csv", "metadata.json"]
# Festivos de Colombia dentro del corte (Ley Emiliani: Asunción 15-ago se traslada al lunes 17).
HOLIDAYS = {"2026-08-07": "Batalla de Boyacá", "2026-08-17": "Asunción de la Virgen"}
EVENT_FREE = 0.01  # intensidad por debajo de la cual el slot se considera sin evento

# --- Tokens visuales (paleta validada: categórica, secuencial azul, divergente azul-rojo) ---
SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]
SEQ = LinearSegmentedColormap.from_list("seq", ["#f4f8fd"] + BLUE)
DIV = LinearSegmentedColormap.from_list("div", ["#9f2d2c", "#e34948", "#f0efec", "#2a78d6", "#104281"])
NEG, POS = "#e34948", "#2a78d6"

ARCHETYPES = [
    "Portal (origen AM)",
    "Intercambio (doble pico)",
    "Oficinas (destino AM)",
    "Universitaria (mediodía)",
    "Ocio nocturno",
]
ARCH_COLOR = dict(zip(ARCHETYPES, SERIES))
DAY_LABELS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "axes.edgecolor": AXIS,
        "axes.linewidth": 0.8,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "axes.axisbelow": True,
        "axes.labelcolor": INK2,
        "axes.titlecolor": INK,
        "axes.titlesize": 10.5,
        "axes.titleweight": "bold",
        "axes.titlelocation": "left",
        "axes.titlepad": 10,
        "xtick.color": AXIS,
        "ytick.color": AXIS,
        "xtick.labelcolor": INK2,
        "ytick.labelcolor": INK2,
        "font.size": 9,
        "font.family": "DejaVu Sans",
        "legend.frameon": False,
        "legend.fontsize": 8.5,
        "lines.linewidth": 2,
        "lines.solid_capstyle": "round",
    }
)

FIGURES: list[str] = []


def save(fig: plt.Figure, name: str) -> None:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIG_DIR / f"{name}.png", dpi=160, bbox_inches="tight")
    plt.close(fig)
    FIGURES.append(name)


def heat_axes(ax: plt.Axes) -> None:
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(length=0)


def day_month(ts: pd.Timestamp) -> str:
    return f"{ts.day:02d}-{MONTHS[ts.month - 1]}"


def date_axis(ax: plt.Axes) -> None:
    ax.xaxis.set_major_locator(matplotlib.dates.WeekdayLocator(byweekday=0))
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _: day_month(matplotlib.dates.num2date(x))))


def hour_axis(ax: plt.Axes) -> None:
    ax.set_xlim(0, 24)
    ax.set_xticks([0, 6, 12, 18, 24])
    ax.set_xticklabels(["0h", "6h", "12h", "18h", "24h"])


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------


def load_data(refresh: bool) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    meta_path = DATA_DIR / "api_meta.json"
    try:
        with PulsoTransmiClient() as client:
            meta = client.meta()
            for filename in FILES:
                if refresh or not (DATA_DIR / filename).exists():
                    client.download(filename, DATA_DIR / filename)
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    except PulsoTransmiError as exc:
        if refresh or not meta_path.exists():
            raise
        print(f"API no disponible ({exc}); se usa la copia local verificada por SHA-256.")
        meta = json.loads(meta_path.read_text())
        for filename, info in meta["dataset"]["files"].items():
            if hashlib.sha256((DATA_DIR / filename).read_bytes()).hexdigest() != info["sha256"]:
                raise PulsoTransmiError(f"checksum local inválido para {filename}; ejecuta con --refresh") from exc
    stations = pd.read_csv(DATA_DIR / "stations.csv", dtype={"station_id": "string"})
    observations = pd.read_csv(DATA_DIR / "observations.csv", dtype={"station_id": "string"})
    context = pd.read_csv(DATA_DIR / "context.csv")
    return stations, observations, context, meta


def integrity(stations: pd.DataFrame, observations: pd.DataFrame, context: pd.DataFrame, meta: dict) -> dict:
    dataset = meta["dataset"]
    expected = pd.date_range(dataset["history_start"], dataset["history_end"], freq="15min").tz_convert("UTC")
    rows = []
    for station_id, group in observations.groupby("station_id"):
        ts = pd.to_datetime(group["observed_at"], utc=True)
        rows.append(
            {
                "station_id": station_id,
                "filas": len(group),
                "esperadas": len(expected),
                "duplicados": int(ts.duplicated().sum()),
                "faltantes": len(expected.difference(pd.DatetimeIndex(ts))),
                "fuera_de_grilla": int((~ts.isin(expected)).sum()),
                "nulos": int(group.isna().sum().sum()),
                "demanda_cero": int((group["demand"] == 0).sum()),
                "demanda_negativa": int((group["demand"] < 0).sum()),
            }
        )
    table = pd.DataFrame(rows)
    table.to_csv(OUT_DIR / "integridad_observaciones.csv", index=False)

    ctx_ts = pd.to_datetime(context["observed_at"], utc=True)
    ctx = {
        "filas": len(context),
        "faltantes": len(expected.difference(pd.DatetimeIndex(ctx_ts))),
        "duplicados": int(ctx_ts.duplicated().sum()),
        "nulos": int(context.isna().sum().sum()),
        "lluvia_negativa": int((context["rain_mm"] < 0).sum() + (context["rain_forecast"] < 0).sum()),
        "evento_fuera_0_1": int((~context["event_intensity"].between(0, 1)).sum()),
        "estaciones_sin_catalogo": int((~observations["station_id"].isin(stations["station_id"])).sum()),
    }
    return {
        "observaciones_filas": int(table["filas"].sum()),
        "observaciones_duplicados": int(table["duplicados"].sum()),
        "observaciones_faltantes": int(table["faltantes"].sum()),
        "observaciones_nulos": int(table["nulos"].sum()),
        "demanda_cero": int(table["demanda_cero"].sum()),
        "contexto": ctx,
        "ids_con_cero_inicial": int(stations["station_id"].str.startswith("0").sum()),
    }


def build_frame(stations: pd.DataFrame, observations: pd.DataFrame, context: pd.DataFrame) -> pd.DataFrame:
    obs, ctx = observations.copy(), context.copy()
    for frame in (obs, ctx):
        frame["ts"] = pd.to_datetime(frame["observed_at"], utc=True).dt.tz_convert(TZ)
    df = obs.merge(ctx.drop(columns="observed_at"), on="ts", how="left", validate="many_to_one")
    df = df.merge(stations, on="station_id", how="left", validate="many_to_one")
    df["date"] = df["ts"].dt.tz_localize(None).dt.normalize()
    df["dow"] = df["ts"].dt.dayofweek
    df["weekend"] = (df["dow"] >= 5).astype(int)
    df["slot"] = df["ts"].dt.hour * 4 + df["ts"].dt.minute // 15
    df["hour"] = df["slot"] / 4
    df["is_friday"] = (df["dow"] == 4).astype(int)
    df["is_sunday"] = (df["dow"] == 6).astype(int)
    df["is_holiday"] = df["date"].dt.strftime("%Y-%m-%d").isin(HOLIDAYS).astype(int)
    df["trend_days"] = (df["ts"] - df["ts"].min()).dt.total_seconds() / 86400
    df["log_demand"] = np.log(df["demand"])
    df["short"] = df["station_name"].map(lambda name: re.split(r"\s[-–]\s", name)[0])
    return df.sort_values(["station_id", "ts"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Análisis
# ---------------------------------------------------------------------------


def classify_profile(profile: pd.Series) -> str:
    """Regla transparente sobre el perfil laboral medio (índice = slot de 15 min)."""
    am = profile.loc[20:39].max()  # 05:00-09:45
    midday = profile.loc[44:55].max()  # 11:00-13:45
    pm = profile.loc[60:75].max()  # 15:00-18:45
    night = profile.loc[76:91].max()  # 19:00-22:45
    if night > max(am, pm):
        return "Ocio nocturno"
    if midday > max(am, pm):
        return "Universitaria (mediodía)"
    ratio = am / pm
    if ratio > 1.4:
        return "Portal (origen AM)"
    if ratio < 0.85:
        return "Oficinas (destino AM)"
    return "Intercambio (doble pico)"


def station_catalog(df: pd.DataFrame, stations: pd.DataFrame) -> pd.DataFrame:
    weekday = df[df["weekend"] == 0].groupby(["station_id", "slot"])["demand"].mean().unstack()
    catalog = stations.copy()
    catalog["short"] = catalog["station_name"].map(lambda name: re.split(r"\s[-–]\s", name)[0])
    catalog["archetype"] = catalog["station_id"].map(lambda sid: classify_profile(weekday.loc[sid]))
    summary = df.groupby("station_id")["demand"].agg(
        mean="mean", std="std", median="median", p05=lambda s: s.quantile(0.05), p95=lambda s: s.quantile(0.95), max="max"
    )
    daily = df.groupby(["station_id", "date", "weekend"])["demand"].sum().reset_index()
    summary["daily_weekday"] = daily[daily["weekend"] == 0].groupby("station_id")["demand"].mean()
    summary["daily_weekend"] = daily[daily["weekend"] == 1].groupby("station_id")["demand"].mean()
    summary["weekend_index"] = summary["daily_weekend"] / summary["daily_weekday"]
    catalog = catalog.merge(summary.reset_index(), on="station_id")
    catalog["arch_order"] = catalog["archetype"].map(ARCHETYPES.index)
    return catalog.sort_values(["arch_order", "mean"], ascending=[True, False]).reset_index(drop=True)


def station_profiles(df: pd.DataFrame) -> pd.DataFrame:
    clean = df[df["event_intensity"] < EVENT_FREE]
    profiles = (
        clean.groupby(["station_id", "weekend", "slot"])["demand"]
        .agg(
            median="median",
            mean="mean",
            p10=lambda s: s.quantile(0.10),
            p90=lambda s: s.quantile(0.90),
            n="size",
        )
        .reset_index()
    )
    profiles.insert(1, "day_type", np.where(profiles.pop("weekend") == 1, "fin_de_semana", "laboral"))
    profiles.to_csv(OUT_DIR / "perfiles_estacion_slot.csv", index=False)
    return profiles


def fit_station_models(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    full_formula = (
        "log_demand ~ C(slot)*C(weekend) + is_friday + is_sunday + is_holiday"
        " + rain_mm + event_intensity + temperature_c + trend_days"
    )
    variants = {
        "base": "log_demand ~ C(slot)*C(weekend) + event_intensity",
        "rain_mm": "log_demand ~ C(slot)*C(weekend) + event_intensity + rain_mm",
        "rain_forecast": "log_demand ~ C(slot)*C(weekend) + event_intensity + rain_forecast",
        "ambas": "log_demand ~ C(slot)*C(weekend) + event_intensity + rain_mm + rain_forecast",
    }
    rows, resid_full, resid_norain = [], [], []
    for station_id, group in df.groupby("station_id"):
        model = smf.ols(full_formula, data=group).fit()
        ci = model.conf_int()
        row = {"station_id": station_id, "r2": model.rsquared, "resid_sd": np.sqrt(model.scale)}
        for term in ["rain_mm", "event_intensity", "temperature_c", "trend_days", "is_friday", "is_sunday", "is_holiday"]:
            row[f"{term}_coef"] = model.params[term]
            row[f"{term}_lo"] = ci.loc[term, 0]
            row[f"{term}_hi"] = ci.loc[term, 1]
            row[f"{term}_p"] = model.pvalues[term]
        for name, formula in variants.items():
            fitted = smf.ols(formula, data=group).fit()
            row[f"r2_{name}"] = fitted.rsquared
            if name == "base":
                resid_norain.append(fitted.resid)
        resid_full.append(model.resid)
        rows.append(row)
    coefs = pd.DataFrame(rows)
    coefs.to_csv(OUT_DIR / "modelo_log_lineal_por_estacion.csv", index=False)
    return coefs, pd.concat(resid_full).sort_index(), pd.concat(resid_norain).sort_index()


def detect_events(context: pd.DataFrame) -> pd.DataFrame:
    ctx = context.assign(ts=pd.to_datetime(context["observed_at"], utc=True).dt.tz_convert(TZ)).sort_values("ts")
    intensity = ctx["event_intensity"].to_numpy()
    peaks, _ = find_peaks(intensity, height=0.05)

    def gaussian(x: np.ndarray, amp: float, mu: float, sigma: float) -> np.ndarray:
        return amp * np.exp(-((x - mu) ** 2) / (2 * sigma**2))

    rows = []
    for peak in peaks:
        lo, hi = max(peak - 60, 0), min(peak + 61, len(intensity))
        x = np.arange(lo, hi) - peak
        (amp, mu, sigma), _ = curve_fit(gaussian, x, intensity[lo:hi], p0=[1, 0, 10])
        ts = ctx["ts"].iloc[peak]
        rows.append(
            {
                "pico": ts.isoformat(),
                "dia": DAY_LABELS[ts.dayofweek],
                "intensidad_max": round(float(intensity[peak]), 3),
                "sigma_horas": round(abs(sigma) / 4, 2),
                "ventana_intensidad_mayor_0_5_horas": round(float((intensity[lo:hi] > 0.5).sum()) / 4, 2),
            }
        )
    events = pd.DataFrame(rows)
    events.to_csv(OUT_DIR / "eventos_detectados.csv", index=False)
    return events


def variance_decomposition(df: pd.DataFrame) -> pd.DataFrame:
    y = df["log_demand"]
    sst = ((y - y.mean()) ** 2).sum()

    def r2(pred: pd.Series) -> float:
        return 1 - ((y - pred) ** 2).sum() / sst

    def per_station_fit(residual: pd.Series, column: str) -> pd.Series:
        fitted = pd.Series(0.0, index=df.index)
        for _, idx in df.groupby("station_id").groups.items():
            slope, intercept = np.polyfit(df.loc[idx, column], residual.loc[idx], 1)
            fitted.loc[idx] = intercept + slope * df.loc[idx, column]
        return fitted

    steps = []
    pred = df.groupby("station_id")["log_demand"].transform("mean")
    steps.append(("Estación (nivel medio)", r2(pred)))
    pred = df.groupby(["station_id", "slot"])["log_demand"].transform("mean")
    steps.append(("Hora del día × estación", r2(pred)))
    pred = df.groupby(["station_id", "weekend", "slot"])["log_demand"].transform("mean")
    steps.append(("Laboral / fin de semana", r2(pred)))
    pred = pred + per_station_fit(y - pred, "rain_mm")
    steps.append(("Lluvia", r2(pred)))
    pred = pred + per_station_fit(y - pred, "event_intensity")
    steps.append(("Evento", r2(pred)))
    pred = pred + (y - pred).groupby([df["station_id"], df["date"]]).transform("mean")
    steps.append(("Shock diario (no observable ex ante)", r2(pred)))

    table, previous = [], 0.0
    for name, value in steps:
        table.append({"componente": name, "r2_acumulado": value, "aporte_pct": 100 * (value - previous)})
        previous = value
    table.append({"componente": "Ruido residual", "r2_acumulado": 1.0, "aporte_pct": 100 * (1 - previous)})
    result = pd.DataFrame(table)
    result.to_csv(OUT_DIR / "descomposicion_varianza.csv", index=False)
    return result


def wape_accuracy(frame: pd.DataFrame, column: str) -> pd.Series:
    error = (frame["demand"] - frame[column]).abs().groupby(frame["station_id"]).sum()
    wape = error / frame.groupby("station_id")["demand"].sum()
    return 100 * (1 - wape).clip(lower=0)


def baselines(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    cutoff = df["ts"].max() - pd.Timedelta(days=7)
    train, valid = df[df["ts"] <= cutoff], df[df["ts"] > cutoff].copy()
    grouped = df.groupby("station_id")["demand"]
    valid["Ingenuo t−1 día"] = grouped.shift(96).loc[valid.index]
    valid["Ingenuo t−7 días"] = grouped.shift(672).loc[valid.index]
    profile = train.groupby(["station_id", "weekend", "slot"])["demand"].median().rename("Perfil histórico (mediana)")
    valid = valid.join(profile, on=["station_id", "weekend", "slot"])

    formulas = {
        "Log-lineal: perfil + lluvia pronosticada + evento": "log_demand ~ C(slot)*C(weekend) + rain_forecast + event_intensity",
        "Log-lineal: perfil + lluvia real + evento": "log_demand ~ C(slot)*C(weekend) + rain_mm + event_intensity",
    }
    for station_id, group in train.groupby("station_id"):
        idx = valid.index[valid["station_id"] == station_id]
        for name, formula in formulas.items():
            model = smf.ols(formula, data=group).fit()
            # exp(media log) = mediana: el estimador que minimiza el error absoluto (WAPE).
            valid.loc[idx, name] = np.exp(model.predict(valid.loc[idx]))
    best_real = "Log-lineal: perfil + lluvia real + evento"
    log_resid = np.log(valid["demand"]) - np.log(valid[best_real])
    valid["Cota: conoce el shock diario"] = valid[best_real] * np.exp(
        log_resid.groupby([valid["station_id"], valid["date"]]).transform("median")
    )

    methods = [
        "Ingenuo t−1 día",
        "Ingenuo t−7 días",
        "Perfil histórico (mediana)",
        "Log-lineal: perfil + lluvia pronosticada + evento",
        best_real,
        "Cota: conoce el shock diario",
    ]
    per_station = pd.DataFrame({method: wape_accuracy(valid, method) for method in methods})
    summary = pd.DataFrame({"metodo": methods, "accuracy": [per_station[m].mean() for m in methods]})
    summary.to_csv(OUT_DIR / "baselines_validacion_7d.csv", index=False)
    per_station.to_csv(OUT_DIR / "baselines_por_estacion.csv")

    feasible = "Log-lineal: perfil + lluvia pronosticada + evento"
    valid["abs_error"] = (valid["demand"] - valid[feasible]).abs()
    by_hour = valid.groupby(valid["ts"].dt.hour).agg(abs_error=("abs_error", "sum"), demand=("demand", "sum"))
    by_hour["wape"] = by_hour["abs_error"] / by_hour["demand"]
    by_hour["error_share"] = by_hour["abs_error"] / by_hour["abs_error"].sum()
    return summary, per_station, by_hour


# ---------------------------------------------------------------------------
# Figuras
# ---------------------------------------------------------------------------


def fig_map(catalog: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(7.2, 7.6))
    offsets = {
        "07105": (-12, 8, "right"),
        "07107": (12, -8, "left"),
        "10009": (12, 6, "left"),
        "06111": (12, -10, "left"),
        "07111": (0, -30, "center"),
        "05100": (0, -32, "center"),
        "05000": (0, 28, "center"),
    }
    for row in catalog.itertuples():
        ax.scatter(
            row.longitude, row.latitude, s=row.mean * 1.1, color=ARCH_COLOR[row.archetype],
            edgecolor=SURFACE, linewidth=2, zorder=3, alpha=0.95,
        )
        dx, dy, ha = offsets.get(row.station_id, (9, 4, "left"))
        ax.annotate(
            f"{row.short}\n{row.mean:,.0f} pax/15 min".replace(",", "."),
            (row.longitude, row.latitude), xytext=(dx, dy), textcoords="offset points",
            ha=ha, va="center", fontsize=8, color=INK, linespacing=1.25,
        )
    ax.set_aspect(1 / np.cos(np.radians(catalog["latitude"].mean())))
    ax.set_xlabel("Longitud")
    ax.set_ylabel("Latitud")
    ax.margins(0.18)
    ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(5))
    handles = [Line2D([], [], marker="o", ls="", color=ARCH_COLOR[a], markersize=8, label=a) for a in ARCHETYPES]
    ax.legend(handles=handles, loc="upper left", title="Arquetipo (por perfil horario)", title_fontsize=8.5, alignment="left")
    ax.set_title("Ubicación, demanda media y arquetipo de las 12 estaciones")
    save(fig, "01_mapa_estaciones")


def fig_distribution(df: pd.DataFrame, catalog: pd.DataFrame) -> None:
    order = catalog.sort_values("median")["station_id"].tolist()
    names = catalog.set_index("station_id")["short"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 5.2), gridspec_kw={"width_ratios": [1.5, 1]})
    ax = axes[0]
    data = [df.loc[df["station_id"] == sid, "demand"].to_numpy() for sid in order]
    ax.boxplot(
        data, vert=False, widths=0.55, patch_artist=True, whis=(1, 99),
        boxprops={"facecolor": BLUE[0], "edgecolor": BLUE[4], "linewidth": 1},
        medianprops={"color": BLUE[8], "linewidth": 2},
        whiskerprops={"color": BLUE[4], "linewidth": 1}, capprops={"color": BLUE[4], "linewidth": 1},
        flierprops={"marker": "o", "markersize": 2, "markerfacecolor": BLUE[3], "markeredgecolor": "none", "alpha": 0.4},
    )
    ax.set_yticks(range(1, len(order) + 1), [names[s] for s in order])
    ax.set_xscale("log")
    ax.set_xlabel("Demanda por intervalo de 15 min (escala log; bigotes P1–P99)")
    ax.set_title("Distribución de la demanda por estación")
    ax.grid(axis="y", visible=False)

    ax = axes[1]
    sub = catalog.sort_values("weekend_index")
    ax.barh(sub["short"], 100 * sub["weekend_index"], color=SERIES[0], height=0.6)
    ax.axvline(100, color=AXIS, linewidth=1)
    for y, value in enumerate(100 * sub["weekend_index"]):
        ax.text(value + 1, y, f"{value:.0f}", va="center", fontsize=8, color=INK2)
    ax.set_xlim(0, 100 * sub["weekend_index"].max() * 1.12)
    ax.set_xlabel("Demanda diaria fin de semana / laboral (%)")
    ax.set_title("Caída de fin de semana")
    ax.grid(axis="y", visible=False)
    fig.tight_layout(w_pad=3)
    save(fig, "02_distribucion_demanda")


def fig_daily_series(df: pd.DataFrame) -> dict:
    daily = df.groupby(["date", "dow"])["demand"].sum().reset_index()
    weekdays = daily[daily["dow"] < 5]
    holiday_dates = pd.to_datetime(list(HOLIDAYS))
    regular = weekdays[~weekdays["date"].isin(holiday_dates)]
    z_scores = {}
    fig, ax = plt.subplots(figsize=(11.5, 4.2))
    colors = np.where(daily["dow"] >= 5, SERIES[1], SERIES[0])
    ax.bar(daily["date"], daily["demand"] / 1000, color=colors, width=0.75)
    for date_str, name in HOLIDAYS.items():
        date = pd.Timestamp(date_str)
        value = daily.loc[daily["date"] == date, "demand"].iloc[0]
        dow = date.dayofweek
        same_dow = regular[regular["dow"] == dow]["demand"]
        z_scores[date_str] = float((value - regular["demand"].mean()) / regular["demand"].std())
        ax.annotate(
            f"Festivo: {name}\n{(value / same_dow.mean() - 1) * 100:+.1f}% vs. {DAY_LABELS[dow].lower()} típico",
            (date, value / 1000), xytext=(0, 26), textcoords="offset points", ha="center", fontsize=8, color=INK,
            arrowprops={"arrowstyle": "-", "color": MUTED, "linewidth": 0.8},
        )
    ax.set_ylim(0, daily["demand"].max() / 1000 * 1.22)
    ax.set_ylabel("Pasajeros por día (miles, 12 estaciones)")
    ax.legend(handles=[Patch(color=SERIES[0], label="Laboral"), Patch(color=SERIES[1], label="Sábado / domingo")], loc="upper left", ncols=2)
    date_axis(ax)
    ax.set_title("Demanda diaria total: patrón semanal estable y festivos sin efecto")
    ax.grid(axis="x", visible=False)
    save(fig, "03_serie_diaria")
    return z_scores


def fig_hour_heatmap(df: pd.DataFrame, catalog: pd.DataFrame) -> None:
    order = catalog["station_id"].tolist()
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.4), sharey=True)
    for ax, weekend, title in zip(axes, [0, 1], ["Día laboral", "Fin de semana"]):
        hourly = df[df["weekend"] == weekend].groupby(["station_id", df["ts"].dt.hour])["demand"].mean().unstack()
        share = hourly.div(hourly.sum(axis=1), axis=0).loc[order] * 100
        image = ax.imshow(share.to_numpy(), aspect="auto", cmap=SEQ, vmin=0, vmax=12)
        heat_axes(ax)
        ax.set_xticks(range(0, 24, 3), [f"{h}h" for h in range(0, 24, 3)])
        ax.set_yticks(range(len(order)), catalog["short"])
        ax.set_title(title)
        boundaries = catalog["arch_order"].diff().fillna(0).to_numpy().nonzero()[0]
        for b in boundaries:
            ax.axhline(b - 0.5, color=SURFACE, linewidth=3)
    cbar = fig.colorbar(image, ax=axes, shrink=0.8, pad=0.02)
    cbar.set_label("% de la demanda diaria de la estación", color=INK2)
    cbar.outline.set_visible(False)
    save(fig, "04_heatmap_hora_estacion")


def fig_station_profiles(df: pd.DataFrame, catalog: pd.DataFrame) -> None:
    fig, axes = plt.subplots(3, 4, figsize=(13, 8.6), sharex=True)
    for ax, row in zip(axes.flat, catalog.itertuples()):
        g = df[df["station_id"] == row.station_id]
        for weekend, color, label in [(0, SERIES[0], "Laboral (media y banda P10–P90)"), (1, SERIES[1], "Fin de semana (media)")]:
            stats_slot = g[g["weekend"] == weekend].groupby("slot")["demand"].agg(
                mean="mean", p10=lambda s: s.quantile(0.1), p90=lambda s: s.quantile(0.9)
            )
            x = stats_slot.index / 4
            if weekend == 0:
                ax.fill_between(x, stats_slot["p10"], stats_slot["p90"], color=color, alpha=0.15, linewidth=0)
            ax.plot(x, stats_slot["mean"], color=color, linewidth=1.6, label=label)
        ax.set_title(row.short, fontsize=9.5)
        ax.text(0.0, 1.0, row.archetype, transform=ax.transAxes, fontsize=7.5, color=MUTED, va="bottom")
        ax.title.set_position((0, 1.08))
        hour_axis(ax)
        ax.set_ylim(bottom=0)
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper left", ncols=2, bbox_to_anchor=(0.01, 1.03))
    fig.supylabel("Pasajeros por 15 min", color=INK2, fontsize=9)
    fig.tight_layout(h_pad=2.2)
    save(fig, "05_perfiles_por_estacion")


def fig_archetypes(df: pd.DataFrame, catalog: pd.DataFrame) -> float:
    shape = []
    for sid in catalog["station_id"]:
        g = df[df["station_id"] == sid]
        prof = g.groupby(["weekend", "slot"])["demand"].mean()
        shape.append((prof / prof.mean()).to_numpy())
    shape = np.vstack(shape)
    links = linkage(shape, method="ward")
    clusters = fcluster(links, t=5, criterion="maxclust")
    agreement = float(adjusted_rand_score(catalog["archetype"], clusters))

    fig, axes = plt.subplots(1, 2, figsize=(12.5, 5), gridspec_kw={"width_ratios": [1, 1.5]})
    ax = axes[0]
    tree = dendrogram(links, labels=catalog["short"].tolist(), orientation="left", ax=ax, link_color_func=lambda _: MUTED)
    heat_axes(ax)
    ax.set_xticks([])
    leaves = tree["ivl"]
    arch_by_short = catalog.set_index("short")["archetype"]
    for y, name in zip(np.arange(5, 10 * len(leaves), 10), leaves):
        ax.scatter([0], [y], s=40, color=ARCH_COLOR[arch_by_short[name]], zorder=3, clip_on=False)
    ax.set_title(f"Clustering jerárquico (Ward) de la forma del perfil\nacuerdo con la regla de arquetipos: ARI = {agreement:.2f}")

    ax = axes[1]
    weekday = df[df["weekend"] == 0].groupby(["station_id", "slot"])["demand"].mean().unstack()
    normalized = weekday.div(weekday.mean(axis=1), axis=0)
    for archetype in ARCHETYPES:
        ids = catalog.loc[catalog["archetype"] == archetype, "station_id"]
        centroid = normalized.loc[ids].mean()
        x = centroid.index / 4
        ax.plot(x, centroid, color=ARCH_COLOR[archetype], linewidth=2, label=f"{archetype} ({len(ids)})")
    ax.legend(loc="upper left", title="Arquetipo (n estaciones)", title_fontsize=8.5, alignment="left")
    hour_axis(ax)
    ax.set_ylim(0, normalized.max().max() * 1.12)
    ax.set_ylabel("Demanda / media de la estación (día laboral)")
    ax.set_title("Perfil normalizado medio de cada arquetipo")
    fig.tight_layout(w_pad=3)
    save(fig, "06_arquetipos")
    return agreement


def fig_day_of_week(df: pd.DataFrame, catalog: pd.DataFrame) -> pd.DataFrame:
    daily = df.groupby(["station_id", "date", "dow"])["demand"].sum().reset_index()
    by_dow = daily.groupby(["station_id", "dow"])["demand"].mean().unstack()
    index = by_dow.div(by_dow[[0, 1, 2, 3, 4]].mean(axis=1), axis=0).loc[catalog["station_id"]] * 100
    fig, ax = plt.subplots(figsize=(7.5, 5.6))
    norm = TwoSlopeNorm(vmin=index.min().min(), vcenter=100, vmax=100 + (100 - index.min().min()))
    ax.imshow(index.to_numpy(), cmap=DIV, norm=norm, aspect="auto")
    heat_axes(ax)
    for (i, j), value in np.ndenumerate(index.to_numpy()):
        ax.text(j, i, f"{value:.0f}", ha="center", va="center", fontsize=8, color=SURFACE if value < 80 else INK)
    ax.set_xticks(range(7), DAY_LABELS)
    ax.set_yticks(range(len(catalog)), catalog["short"])
    ax.xaxis.tick_top()
    ax.set_title("Índice de demanda diaria por día de la semana (media lun–vie = 100)", pad=26)
    save(fig, "07_dia_de_semana")
    index.to_csv(OUT_DIR / "indice_dia_semana.csv")
    return index


def fig_context(context_ts: pd.DataFrame) -> dict:
    ctx = context_ts
    fig, axes = plt.subplots(2, 3, figsize=(13, 7.6))
    ax = axes[0, 0]
    daily_rain = ctx["rain_mm"].resample("D").sum()
    ax.bar(daily_rain.index, daily_rain, color=SERIES[0], width=0.75)
    ax.set_ylabel("mm acumulados")
    ax.set_title("Lluvia diaria acumulada")
    date_axis(ax)
    ax.grid(axis="x", visible=False)

    ax = axes[0, 1]
    hourly = ctx.groupby(ctx.index.hour)["rain_mm"].agg(["mean", lambda s: (s > 1).mean()])
    ax.bar(hourly.index + 0.5, hourly["mean"], color=SERIES[0], width=0.8)
    hour_axis(ax)
    ax.set_ylabel("mm por 15 min (media)")
    ax.set_title("Lluvia por hora: sin ciclo diurno marcado")
    ax.grid(axis="x", visible=False)

    ax = axes[0, 2]
    temp = ctx.groupby(ctx.index.hour + ctx.index.minute / 60)["temperature_c"].agg(
        mean="mean", p10=lambda s: s.quantile(0.1), p90=lambda s: s.quantile(0.9)
    )
    ax.fill_between(temp.index, temp["p10"], temp["p90"], color=SERIES[1], alpha=0.18, linewidth=0)
    ax.plot(temp.index, temp["mean"], color=SERIES[1])
    hour_axis(ax)
    ax.set_ylabel("°C")
    ax.set_title("Temperatura: ciclo diurno casi determinista")

    ax = axes[1, 0]
    hb = ax.hexbin(ctx["rain_mm"], ctx["rain_forecast"], gridsize=40, cmap=SEQ, bins="log", mincnt=1, linewidths=0)
    lim = max(ctx["rain_mm"].max(), ctx["rain_forecast"].max())
    ax.plot([0, lim], [0, lim], color=MUTED, linewidth=1)
    ax.set_xlabel("Lluvia observada (mm)")
    ax.set_ylabel("Lluvia pronosticada (mm)")
    rain_r = ctx["rain_mm"].corr(ctx["rain_forecast"])
    ax.set_title(f"Pronóstico de lluvia vs. observado (r = {rain_r:.2f})")
    ax.grid(False)

    ax = axes[1, 1]
    ax.hexbin(ctx["temperature_c"], ctx["temperature_forecast"], gridsize=40, cmap=SEQ, bins="log", mincnt=1, linewidths=0)
    lo, hi = ctx[["temperature_c", "temperature_forecast"]].min().min(), ctx[["temperature_c", "temperature_forecast"]].max().max()
    ax.plot([lo, hi], [lo, hi], color=MUTED, linewidth=1)
    temp_r = ctx["temperature_c"].corr(ctx["temperature_forecast"])
    ax.set_xlabel("Temperatura observada (°C)")
    ax.set_ylabel("Temperatura pronosticada (°C)")
    ax.set_title(f"Pronóstico de temperatura vs. observado (r = {temp_r:.2f})")
    ax.grid(False)

    ax = axes[1, 2]
    lags = np.arange(0, 97)
    rain_acf = acf(ctx["rain_mm"], nlags=96, fft=True)
    ax.plot(lags / 4, rain_acf, color=SERIES[0])
    ax.axhline(0, color=AXIS, linewidth=0.8)
    ax.set_xlabel("Rezago (horas)")
    ax.set_ylabel("Autocorrelación")
    half_life = float(np.argmax(rain_acf < 0.5) / 4)
    ax.set_title(f"Persistencia de la lluvia (ACF < 0.5 a las {half_life:.2g} h)")
    fig.tight_layout(h_pad=2.5, w_pad=2.5)
    save(fig, "08_contexto_clima")
    forecast_shift = {k: float(ctx["rain_mm"].corr(ctx["rain_forecast"].shift(k))) for k in range(-4, 5)}
    return {
        "lluvia_diaria_media_mm": float(daily_rain.mean()),
        "lluvia_diaria_max_mm": float(daily_rain.max()),
        "lluvia_corr_pronostico": float(rain_r),
        "lluvia_pronostico_sesgo_mm": float((ctx["rain_forecast"] - ctx["rain_mm"]).mean()),
        "lluvia_pronostico_mae_mm": float((ctx["rain_forecast"] - ctx["rain_mm"]).abs().mean()),
        "lluvia_pronostico_mejor_desfase_slots": int(max(forecast_shift, key=forecast_shift.get)),
        "lluvia_vida_media_horas": half_life,
        "temperatura_corr_pronostico": float(temp_r),
        "temperatura_pronostico_mae_c": float((ctx["temperature_forecast"] - ctx["temperature_c"]).abs().mean()),
        "temperatura_sd_media_diaria_c": float(ctx["temperature_c"].resample("D").mean().std()),
        "temperatura_r2_explicado_por_hora": float(
            1 - ((ctx["temperature_c"] - ctx.groupby(ctx.index.hour)["temperature_c"].transform("mean")) ** 2).sum()
            / ((ctx["temperature_c"] - ctx["temperature_c"].mean()) ** 2).sum()
        ),
    }


def fig_context_correlation(df: pd.DataFrame, context_ts: pd.DataFrame) -> pd.DataFrame:
    network = df.groupby("ts").agg(demand=("demand", "sum"), ratio=("ratio", lambda s: np.exp(np.log(s).mean())))
    frame = context_ts.join(network)
    frame["hora_sin"] = np.sin(2 * np.pi * (frame.index.hour + frame.index.minute / 60) / 24)
    frame["hora_cos"] = np.cos(2 * np.pi * (frame.index.hour + frame.index.minute / 60) / 24)
    frame["fin_de_semana"] = (frame.index.dayofweek >= 5).astype(int)
    labels = {
        "demand": "Demanda red",
        "ratio": "Demanda / perfil",
        "rain_mm": "Lluvia obs.",
        "rain_forecast": "Lluvia pron.",
        "temperature_c": "Temp. obs.",
        "temperature_forecast": "Temp. pron.",
        "event_intensity": "Evento",
        "hora_sin": "sin(hora)",
        "hora_cos": "cos(hora)",
        "fin_de_semana": "Fin de semana",
    }
    corr = frame[list(labels)].rename(columns=labels).corr(method="spearman")
    fig, ax = plt.subplots(figsize=(8.4, 7))
    ax.imshow(corr.to_numpy(), cmap=DIV, vmin=-1, vmax=1)
    heat_axes(ax)
    for (i, j), value in np.ndenumerate(corr.to_numpy()):
        ax.text(j, i, f"{value:.2f}", ha="center", va="center", fontsize=7.5, color=SURFACE if abs(value) > 0.6 else INK)
    ax.set_xticks(range(len(corr)), corr.columns, rotation=40, ha="right")
    ax.set_yticks(range(len(corr)), corr.index)
    ax.set_title("Correlación de Spearman: red total vs. contexto (4.320 intervalos)")
    save(fig, "09_correlacion_contexto")
    corr.to_csv(OUT_DIR / "correlacion_contexto_spearman.csv")
    return corr


def fig_rain(df: pd.DataFrame, coefs: pd.DataFrame, catalog: pd.DataFrame, resid_norain: pd.Series) -> dict:
    names = catalog.set_index("station_id")["short"]
    fig, axes = plt.subplots(1, 3, figsize=(14, 5), gridspec_kw={"width_ratios": [1.1, 1, 1]})

    ax = axes[0]
    sub = coefs.assign(effect=100 * (np.exp(coefs["rain_mm_coef"]) - 1)).sort_values("effect")
    lo = 100 * (np.exp(sub["rain_mm_lo"]) - 1)
    hi = 100 * (np.exp(sub["rain_mm_hi"]) - 1)
    y = np.arange(len(sub))
    colors = np.where(sub["effect"] < 0, NEG, POS)
    ax.hlines(y, lo, hi, color=colors, linewidth=2)
    ax.scatter(sub["effect"], y, color=colors, s=46, zorder=3, edgecolor=SURFACE, linewidth=2)
    ax.axvline(0, color=AXIS, linewidth=1)
    ax.set_yticks(y, [names[s] for s in sub["station_id"]])
    ax.set_xlabel("Cambio en demanda por cada mm de lluvia (% , IC 95%)")
    ax.set_title("Efecto de la lluvia (modelo log-lineal por estación)")
    ax.grid(axis="y", visible=False)

    positive = coefs.loc[coefs["rain_mm_coef"] > 0, "station_id"].tolist()
    groups = {
        f"{len(coefs) - len(positive)} estaciones con efecto negativo": ~df["station_id"].isin(positive),
        " y ".join(names[s] for s in positive): df["station_id"].isin(positive),
    }
    clean = df["event_intensity"] < EVENT_FREE
    bins = [-0.01, 0.1, 0.5, 1, 2, 3, 10]
    labels = ["0–0.1", "0.1–0.5", "0.5–1", "1–2", "2–3", ">3"]
    ax = axes[1]
    for (label, mask), color in zip(groups.items(), [NEG, POS]):
        sub = df[mask & clean]
        effect = sub.groupby(pd.cut(sub["rain_mm"], bins, labels=labels), observed=True)["ratio"].apply(
            lambda s: 100 * (np.exp(np.log(s).mean()) - 1)
        )
        ax.plot(range(len(effect)), effect.to_numpy(), color=color, marker="o", markersize=7, markeredgecolor=SURFACE, markeredgewidth=2, label=label)
    counts = pd.cut(df.loc[clean].drop_duplicates("ts")["rain_mm"], bins, labels=labels).value_counts().sort_index()
    ax.set_xticks(range(len(labels)), [f"{l}\n(n={c})" for l, c in zip(labels, counts)], fontsize=7.5)
    ax.axhline(0, color=AXIS, linewidth=1)
    ax.set_xlabel("Lluvia observada en el intervalo (mm)")
    ax.set_ylabel("Demanda vs. perfil típico (%)")
    ax.set_title("Respuesta no lineal por intensidad de lluvia")
    ax.legend(loc="lower left")

    ax = axes[2]
    lags = np.arange(-8, 25)
    frame = df.assign(resid=resid_norain)
    for (label, mask), color in zip(groups.items(), [NEG, POS]):
        curves = []
        for sid, g in frame[mask & clean.to_numpy()].groupby("station_id"):
            series = df.loc[df["station_id"] == sid].set_index("ts")
            r = g.set_index("ts")["resid"]
            curves.append([r.corr(series["rain_mm"].shift(k).reindex(r.index)) for k in lags])
        ax.plot(lags / 4, np.mean(curves, axis=0), color=color, label=label)
    ax.axhline(0, color=AXIS, linewidth=1)
    ax.axvline(0, color=AXIS, linewidth=1)
    ax.set_xlabel("Rezago de la lluvia (horas; negativo = lluvia futura)")
    ax.set_ylabel("Correlación con residuo de demanda")
    ax.set_title("¿Efecto inmediato o retardado?")
    fig.tight_layout(w_pad=2.5)
    save(fig, "10_efecto_lluvia")

    return {
        "lluvia_efecto_pct_por_mm": {names[s]: float(100 * (np.exp(c) - 1)) for s, c in zip(coefs["station_id"], coefs["rain_mm_coef"])},
        "estaciones_lluvia_positiva": [names[s] for s in positive],
        "r2_ganancia_lluvia_real_pp": float(100 * (coefs["r2_rain_mm"] - coefs["r2_base"]).mean()),
        "r2_ganancia_lluvia_pronostico_pp": float(100 * (coefs["r2_rain_forecast"] - coefs["r2_base"]).mean()),
        "r2_ganancia_ambas_pp": float(100 * (coefs["r2_ambas"] - coefs["r2_base"]).mean()),
    }


def fig_events(df: pd.DataFrame, context_ts: pd.DataFrame, coefs: pd.DataFrame, catalog: pd.DataFrame, events: pd.DataFrame) -> dict:
    names = catalog.set_index("station_id")["short"]
    fig = plt.figure(figsize=(13.5, 8.2))
    grid = fig.add_gridspec(2, 3, height_ratios=[0.8, 1.2], hspace=0.45, wspace=0.3)

    ax = fig.add_subplot(grid[0, :])
    ax.fill_between(context_ts.index, context_ts["event_intensity"], color=SERIES[0], alpha=0.2, linewidth=0)
    ax.plot(context_ts.index, context_ts["event_intensity"], color=SERIES[0], linewidth=1.2)
    for event in events.itertuples():
        ts = pd.Timestamp(event.pico)
        ax.annotate(f"{event.dia} {day_month(ts)} {ts:%H:%M}", (ts, 1), xytext=(0, 6), textcoords="offset points", ha="center", fontsize=8, color=INK)
    ax.set_ylim(0, 1.25)
    ax.set_ylabel("event_intensity")
    date_axis(ax)
    ax.set_title("Eventos: 5 pulsos gaussianos centrados a las 19:45, todos en días laborales")
    ax.grid(axis="x", visible=False)

    ax = fig.add_subplot(grid[1, 0])
    sub = coefs.assign(lift=100 * (np.exp(coefs["event_intensity_coef"]) - 1)).sort_values("lift")
    ax.barh([names[s] for s in sub["station_id"]], sub["lift"], color=SERIES[0], height=0.6)
    for y, value in enumerate(sub["lift"]):
        ax.text(value + 0.8, y, f"+{value:.0f}%", va="center", fontsize=8, color=INK2)
    ax.set_xlim(0, sub["lift"].max() * 1.2)
    ax.set_xlabel("Aumento con intensidad = 1 (%)")
    ax.set_title("Impacto del evento por estación")
    ax.grid(axis="y", visible=False)

    event_dates = pd.to_datetime([pd.Timestamp(e).tz_convert(TZ).tz_localize(None).normalize() for e in events["pico"]])
    top = sub["station_id"].iloc[::-1].tolist()[:2]
    for position, sid in enumerate(top, start=1):
        ax = fig.add_subplot(grid[1, position])
        g = df[(df["station_id"] == sid) & (df["weekend"] == 0)]
        on = g[g["date"].isin(event_dates)].groupby("slot")["demand"].mean()
        off = g[~g["date"].isin(event_dates) & (g["is_holiday"] == 0)].groupby("slot")["demand"].agg(
            mean="mean", p10=lambda s: s.quantile(0.1), p90=lambda s: s.quantile(0.9)
        )
        ax.fill_between(off.index / 4, off["p10"], off["p90"], color=MUTED, alpha=0.18, linewidth=0)
        ax.plot(off.index / 4, off["mean"], color=MUTED, linewidth=1.6, label="Laboral sin evento (media, P10–P90)")
        ax.plot(on.index / 4, on.to_numpy(), color=SERIES[1], linewidth=2, label="Días con evento (media de 5)")
        hour_axis(ax)
        ax.set_ylim(bottom=0)
        ax.set_title(f"{names[sid]}: perfil con y sin evento")
        if position == 1:
            ax.legend(loc="upper left", fontsize=7.5)
    save(fig, "11_eventos")
    return {
        "eventos_n": int(len(events)),
        "evento_lift_pct": {names[s]: float(100 * (np.exp(c) - 1)) for s, c in zip(coefs["station_id"], coefs["event_intensity_coef"])},
        "evento_sigma_horas": events["sigma_horas"].tolist(),
    }


def fig_autocorrelation(df: pd.DataFrame, catalog: pd.DataFrame, resid_full: pd.Series) -> dict:
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.6), gridspec_kw={"width_ratios": [1.6, 1]})
    ax = axes[0]
    picks = [catalog.loc[catalog["archetype"] == a, "station_id"].iloc[0] for a in ARCHETYPES[:1] + ARCHETYPES[2:3] + ARCHETYPES[4:5]]
    names = catalog.set_index("station_id")["short"]
    nlags = 96 * 14
    for sid, color in zip(picks, SERIES):
        series = df.loc[df["station_id"] == sid, "log_demand"].to_numpy()
        values = acf(series, nlags=nlags, fft=True)
        ax.plot(np.arange(nlags + 1) / 96, values, color=color, linewidth=1.3, label=names[sid])
    for day in [1, 7, 14]:
        ax.axvline(day, color=AXIS, linewidth=0.8)
    ax.axhline(0, color=AXIS, linewidth=0.8)
    ax.set_xlabel("Rezago (días)")
    ax.set_ylabel("ACF de log(demanda)")
    ax.set_title("Autocorrelación de la serie: estacionalidad diaria y semanal")
    ax.legend(loc="upper right", ncols=3)

    ax = axes[1]
    frame = df.assign(resid=resid_full)
    curves = np.array([acf(g["resid"].to_numpy(), nlags=192, fft=True) for _, g in frame.groupby("station_id")])
    lags = np.arange(1, 193) / 4
    ax.fill_between(lags, curves[:, 1:].min(axis=0), curves[:, 1:].max(axis=0), color=SERIES[0], alpha=0.18, linewidth=0)
    ax.plot(lags, curves[:, 1:].mean(axis=0), color=SERIES[0], linewidth=1.6, label="Media de 12 estaciones (banda: min–max)")
    bound = 1.96 / np.sqrt(len(frame) / 12)
    ax.axhspan(-bound, bound, color=MUTED, alpha=0.15, linewidth=0)
    ax.set_ylim(-0.1, 0.15)
    ax.set_xticks([0, 6, 12, 24, 36, 48])
    ax.set_xlabel("Rezago (horas)")
    ax.set_ylabel("ACF del residuo")
    ax.set_title("Residuo tras perfil + lluvia + evento")
    ax.legend(loc="upper right", fontsize=7.5)
    fig.tight_layout(w_pad=3)
    save(fig, "12_autocorrelacion")
    return {
        "residuo_acf_lag1": float(curves[:, 1].mean()),
        "residuo_acf_lag4": float(curves[:, 4].mean()),
        "residuo_acf_lag96": float(curves[:, 96].mean()),
    }


def fig_station_correlation(df: pd.DataFrame, catalog: pd.DataFrame, resid_full: pd.Series) -> dict:
    order = catalog["station_id"].tolist()
    raw = df.pivot(index="ts", columns="station_id", values="log_demand")[order].corr()
    resid = df.assign(resid=resid_full).pivot(index="ts", columns="station_id", values="resid")[order].corr()
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.8))
    for ax, matrix, title in [
        (axes[0], raw, "log(demanda) cruda: domina el ciclo diario compartido"),
        (axes[1], resid, "Residuo del modelo: casi independiente entre estaciones"),
    ]:
        ax.imshow(matrix.to_numpy(), cmap=DIV, vmin=-1, vmax=1)
        heat_axes(ax)
        for (i, j), value in np.ndenumerate(matrix.to_numpy()):
            ax.text(j, i, f"{value:.2f}", ha="center", va="center", fontsize=6.5, color=SURFACE if abs(value) > 0.6 else INK)
        ax.set_xticks(range(len(order)), catalog["short"], rotation=55, ha="right", fontsize=7.5)
        ax.set_yticks(range(len(order)), catalog["short"], fontsize=7.5)
        ax.set_title(title)
    fig.tight_layout(w_pad=3)
    save(fig, "13_correlacion_estaciones")
    upper = np.triu_indices(len(order), 1)
    return {
        "corr_media_log_demanda_entre_estaciones": float(raw.to_numpy()[upper].mean()),
        "corr_media_residuo_entre_estaciones": float(resid.to_numpy()[upper].mean()),
    }


def fig_daily_shock(df: pd.DataFrame, catalog: pd.DataFrame, resid_full: pd.Series) -> dict:
    frame = df.assign(resid=resid_full)
    shock = frame.groupby(["date", "station_id"])["resid"].mean().unstack()[catalog["station_id"]]
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.6), gridspec_kw={"width_ratios": [2.1, 1]})
    ax = axes[0]
    pct = 100 * (np.exp(shock) - 1)
    limit = np.nanmax(np.abs(pct.to_numpy()))
    image = ax.imshow(pct.T.to_numpy(), cmap=DIV, vmin=-limit, vmax=limit, aspect="auto")
    heat_axes(ax)
    ax.set_yticks(range(len(catalog)), catalog["short"])
    ticks = range(0, len(pct), 4)
    ax.set_xticks(ticks, [day_month(pct.index[i]) for i in ticks], rotation=40, ha="right")
    cbar = fig.colorbar(image, ax=ax, shrink=0.8, pad=0.01)
    cbar.set_label("Nivel del día vs. modelo (%)", color=INK2)
    cbar.outline.set_visible(False)
    upper = np.triu_indices(len(catalog), 1)
    shock_corr = float(shock.corr().to_numpy()[upper].mean())
    ax.set_title(f"Shock diario por estación (correlación media entre estaciones = {shock_corr:.2f})")

    ax = axes[1]
    frame["half"] = np.where(frame["slot"] < 40, "am", "resto")
    halves = frame.groupby(["station_id", "date", "half"])["resid"].mean().unstack()
    r = halves["am"].corr(halves["resto"])
    ax.scatter(100 * (np.exp(halves["am"]) - 1), 100 * (np.exp(halves["resto"]) - 1), s=14, color=SERIES[0], alpha=0.55, edgecolor="none")
    slope, intercept = np.polyfit(halves["am"], halves["resto"], 1)
    x = np.linspace(halves["am"].min(), halves["am"].max(), 10)
    ax.plot(100 * (np.exp(x) - 1), 100 * (np.exp(intercept + slope * x) - 1), color=INK2, linewidth=1.2)
    ax.axhline(0, color=AXIS, linewidth=0.8)
    ax.axvline(0, color=AXIS, linewidth=0.8)
    ax.set_xlabel("Desvío medio antes de las 10:00 (%)")
    ax.set_ylabel("Desvío medio desde las 10:00 (%)")
    ax.set_title(f"La mañana no anticipa el resto del día (r = {r:.2f})")
    fig.tight_layout(w_pad=2.5)
    save(fig, "14_shock_diario")
    persistence = float(np.nanmean([shock[c].autocorr(1) for c in shock]))
    return {
        "shock_diario_sd_pct": float(100 * shock.stack().std()),
        "shock_diario_corr_entre_estaciones": shock_corr,
        "shock_diario_autocorr_dia_anterior": persistence,
        "shock_manana_vs_resto_corr": float(r),
        "shock_manana_vs_resto_pendiente": float(slope),
    }


def fig_noise(df: pd.DataFrame, resid_full: pd.Series) -> dict:
    clean = df[df["event_intensity"] < EVENT_FREE]
    groups = clean.groupby(["station_id", "weekend", "slot"])["demand"].agg(["mean", "var"])
    slope, intercept = np.polyfit(np.log(groups["mean"]), np.log(groups["var"]), 1)
    cv = np.sqrt(groups["var"]) / groups["mean"]

    fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.8))
    ax = axes[0]
    ax.scatter(groups["mean"], groups["var"], s=6, color=SERIES[0], alpha=0.35, edgecolor="none")
    x = np.geomspace(groups["mean"].min(), groups["mean"].max(), 50)
    ax.plot(x, np.exp(intercept) * x**slope, color=INK2, linewidth=1.4, label=f"Ajuste: var ∝ media^{slope:.2f}")
    ax.plot(x, x, color=SERIES[1], linewidth=1.4, label="Poisson: var = media")
    ax.plot(x, (cv.median() * x) ** 2, color=SERIES[2], linewidth=1.4, label=f"CV constante = {cv.median():.2f}")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Media por (estación, tipo de día, slot)")
    ax.set_ylabel("Varianza")
    ax.set_title("Ruido multiplicativo: la varianza crece con el nivel")
    ax.legend(loc="upper left")

    ax = axes[1]
    ax.hist(resid_full, bins=120, density=True, color=SERIES[0], alpha=0.85)
    grid = np.linspace(resid_full.quantile(0.001), resid_full.quantile(0.999), 200)
    ax.plot(grid, stats.norm.pdf(grid, resid_full.mean(), resid_full.std()), color=INK2, linewidth=1.4, label="Normal de referencia")
    kurt, skew = float(stats.kurtosis(resid_full)), float(stats.skew(resid_full))
    ax.set_xlim(grid.min(), grid.max())
    ax.set_xlabel("Residuo en escala log")
    ax.set_title(f"Residuos del modelo log-lineal (sd {resid_full.std():.3f}, asimetría {skew:.2f}, curtosis {kurt:.2f})")
    ax.legend(loc="upper left")
    ax.grid(axis="x", visible=False)
    fig.tight_layout(w_pad=3)
    save(fig, "15_ruido")
    return {
        "ruido_exponente_var_media": float(slope),
        "ruido_cv_mediano": float(cv.median()),
        "residuo_log_sd": float(resid_full.std()),
        "residuo_asimetria": skew,
        "residuo_curtosis_exceso": kurt,
        "var_sobre_media_mediana": float((groups["var"] / groups["mean"]).median()),
    }


def fig_weekly_stability(df: pd.DataFrame, catalog: pd.DataFrame, coefs: pd.DataFrame) -> dict:
    clean = df[df["event_intensity"] < EVENT_FREE].copy()
    clean["week"] = clean["date"] - pd.to_timedelta(clean["dow"], unit="D")
    weekly = clean.groupby(["week", "station_id"])["ratio"].apply(lambda s: 100 * (np.exp(np.log(s).mean()) - 1)).unstack()
    weekly = weekly[catalog["station_id"]]
    days = clean.groupby("week")["date"].nunique()

    fig, axes = plt.subplots(1, 2, figsize=(13.5, 5.2), gridspec_kw={"width_ratios": [1.4, 1]})
    ax = axes[0]
    limit = np.nanmax(np.abs(weekly.to_numpy()))
    image = ax.imshow(weekly.T.to_numpy(), cmap=DIV, vmin=-limit, vmax=limit, aspect="auto")
    heat_axes(ax)
    for (i, j), value in np.ndenumerate(weekly.T.to_numpy()):
        ax.text(j, i, f"{value:+.1f}", ha="center", va="center", fontsize=7.5, color=INK)
    ax.set_xticks(range(len(weekly)), [f"{day_month(w)}\n({days[w]} d)" for w in weekly.index], fontsize=7.5)
    ax.set_yticks(range(len(catalog)), catalog["short"])
    ax.set_title("Demanda semanal vs. perfil del periodo completo (%)")
    cbar = fig.colorbar(image, ax=ax, shrink=0.8, pad=0.01)
    cbar.outline.set_visible(False)

    ax = axes[1]
    names = catalog.set_index("station_id")["short"]
    sub = coefs.assign(
        trend=100 * (np.exp(7 * coefs["trend_days_coef"]) - 1),
        lo=100 * (np.exp(7 * coefs["trend_days_lo"]) - 1),
        hi=100 * (np.exp(7 * coefs["trend_days_hi"]) - 1),
    ).sort_values("trend")
    y = np.arange(len(sub))
    significant = sub["trend_days_p"] < 0.05
    colors = np.where(significant, SERIES[1], MUTED)
    ax.hlines(y, sub["lo"], sub["hi"], color=colors, linewidth=2)
    ax.scatter(sub["trend"], y, color=colors, s=46, zorder=3, edgecolor=SURFACE, linewidth=2)
    ax.axvline(0, color=AXIS, linewidth=1)
    ax.set_yticks(y, [names[s] for s in sub["station_id"]])
    ax.set_xlabel("Tendencia lineal (% por semana, IC 95%)")
    ax.set_title("Deriva lenta: naranja = significativa (p < 0.05)")
    ax.grid(axis="y", visible=False)
    fig.tight_layout(w_pad=3)
    save(fig, "16_estabilidad_semanal")
    weekly.to_csv(OUT_DIR / "desvio_semanal_vs_perfil.csv")
    return {
        "tendencia_pct_semana_media": float(sub["trend"].mean()),
        "tendencia_estaciones_significativas": int(significant.sum()),
        "ultima_semana_desvio_medio_pct": float(weekly.iloc[-1].mean()),
    }


def fig_variance(decomposition: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(10, 4.2))
    rows = decomposition.iloc[::-1]
    colors = [MUTED if "Ruido" in c or "no observable" in c else SERIES[0] for c in rows["componente"]]
    ax.barh(rows["componente"], rows["aporte_pct"], color=colors, height=0.6)
    for y, value in enumerate(rows["aporte_pct"]):
        ax.text(value + 0.6, y, f"{value:.1f}%", va="center", fontsize=8.5, color=INK2)
    ax.set_xlim(0, rows["aporte_pct"].max() * 1.12)
    ax.set_xlabel("% de la varianza de log(demanda) explicada al agregar cada componente (en orden)")
    ax.set_title("¿Qué determina la demanda? Descomposición secuencial de la varianza")
    ax.grid(axis="y", visible=False)
    save(fig, "17_descomposicion_varianza")


def fig_baselines(summary: pd.DataFrame, per_station: pd.DataFrame, by_hour: pd.DataFrame, catalog: pd.DataFrame) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(15, 5), gridspec_kw={"width_ratios": [1.3, 1, 1]})
    ax = axes[0]
    rows = summary.iloc[::-1]
    colors = [MUTED if m.startswith("Cota") else (SERIES[0] if "pronosticada" in m else BLUE[1]) for m in rows["metodo"]]
    ax.barh([m.replace(": ", ":\n") for m in rows["metodo"]], rows["accuracy"], color=colors, height=0.6)
    for y, value in enumerate(rows["accuracy"]):
        ax.text(value + 0.3, y, f"{value:.2f}", va="center", fontsize=8.5, color=INK2)
    ax.set_xlim(70, 100)
    ax.set_xlabel("Accuracy = 100 × (1 − WAPE), promedio de estaciones")
    ax.set_title("Baselines en la última semana (2–8 sep)")
    ax.grid(axis="y", visible=False)
    ax.tick_params(axis="y", labelsize=8)

    ax = axes[1]
    names = catalog.set_index("station_id")["short"]
    best = "Log-lineal: perfil + lluvia pronosticada + evento"
    sub = per_station.sort_values(best)
    y = np.arange(len(sub))
    ax.hlines(y, sub["Perfil histórico (mediana)"], sub[best], color=AXIS, linewidth=1.5)
    ax.scatter(sub["Perfil histórico (mediana)"], y, color=BLUE[1], s=40, zorder=3, edgecolor=SURFACE, linewidth=2, label="Perfil histórico")
    ax.scatter(sub[best], y, color=SERIES[0], s=46, zorder=3, edgecolor=SURFACE, linewidth=2, label="+ lluvia pron. + evento")
    ax.set_yticks(y, [names[s] for s in sub.index])
    ax.set_xlabel("Accuracy por estación")
    ax.set_title("Ganancia por estación")
    ax.legend(loc="lower right", fontsize=7.5)
    ax.grid(axis="y", visible=False)

    ax = axes[2]
    ax.bar(by_hour.index + 0.5, 100 * by_hour["error_share"], color=SERIES[0], width=0.8, label="% del error absoluto total")
    ax.plot(by_hour.index + 0.5, 100 * by_hour["wape"], color=SERIES[1], marker="o", markersize=5, markeredgecolor=SURFACE, label="WAPE de la hora (%)")
    hour_axis(ax)
    ax.set_ylim(0, 16.5)
    ax.set_title("¿Dónde se concentra el error? (mejor modelo factible)")
    ax.legend(loc="upper left", fontsize=7.5, ncols=2, bbox_to_anchor=(0, 1.0))
    ax.grid(axis="x", visible=False)
    fig.tight_layout(w_pad=3)
    save(fig, "18_baselines")


# ---------------------------------------------------------------------------
# MLflow
# ---------------------------------------------------------------------------


def git_state() -> dict:
    def run(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=REPO_DIR, capture_output=True, text=True, check=True).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return "desconocido"

    return {"git_commit": run("rev-parse", "HEAD"), "git_dirty": str(bool(run("status", "--porcelain")))}


def flatten_metrics(findings: dict, prefix: str = "") -> dict[str, float]:
    metrics = {}
    for key, value in findings.items():
        name = re.sub(r"[^0-9A-Za-z_\-./ ]", "_", f"{prefix}{key}")
        if isinstance(value, dict):
            metrics.update(flatten_metrics(value, f"{name}."))
        elif isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool):
            metrics[name] = float(value)
    return metrics


def log_to_mlflow(meta: dict, findings: dict, observations: pd.DataFrame) -> str:
    import mlflow
    import mlflow.data

    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    tracking_uri = os.getenv("MLFLOW_TRACKING_URI", f"sqlite:///{REPO_DIR / 'mlflow.db'}")
    mlflow.set_tracking_uri(tracking_uri)
    experiment = "pulso-transmi-eda"
    if mlflow.get_experiment_by_name(experiment) is None:
        mlflow.create_experiment(experiment, artifact_location=(REPO_DIR / "mlartifacts").as_uri())
    mlflow.set_experiment(experiment)

    dataset = meta["dataset"]
    with mlflow.start_run(run_name=f"eda-{dataset['dataset']}-{dataset['history_end'][:10]}") as run:
        mlflow.set_tags({"etapa": "eda", **git_state()})
        mlflow.log_params(
            {
                "api_version": meta["api_version"],
                "dataset": dataset["dataset"],
                "generated_at": dataset["generated_at"],
                "history_start": dataset["history_start"],
                "history_end": dataset["history_end"],
                "frequency_minutes": dataset["frequency_minutes"],
                "station_count": dataset["station_count"],
                **{f"sha256_{name.split('.')[0]}": info["sha256"] for name, info in dataset["files"].items()},
                "event_free_threshold": EVENT_FREE,
                "validacion": "últimos 7 días",
            }
        )
        mlflow.log_input(
            mlflow.data.from_pandas(
                observations,
                source=f"{os.getenv('PULSO_API_URL', 'https://pulso-transmi.72-60-245-2.sslip.io')}/v1/downloads/observations.csv",
                name=dataset["dataset"],
                digest=dataset["files"]["observations.csv"]["sha256"][:16],
            ),
            context="eda",
        )
        mlflow.log_metrics(flatten_metrics(findings))
        mlflow.log_artifacts(str(FIG_DIR), "figures")
        mlflow.log_artifacts(str(OUT_DIR), "tables")
        for extra in [EDA_DIR / "schema.sql", REPORT_DIR / "index.html"]:
            if extra.exists():
                mlflow.log_artifact(str(extra))
        return run.info.run_id


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="vuelve a descargar los datos de la API")
    parser.add_argument("--no-mlflow", action="store_true", help="no registra el run en MLflow")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    stations, observations, context, meta = load_data(args.refresh)
    findings: dict = {"integridad": integrity(stations, observations, context, meta)}
    df = build_frame(stations, observations, context)
    context_ts = context.assign(ts=pd.to_datetime(context["observed_at"], utc=True).dt.tz_convert(TZ)).set_index("ts").drop(columns="observed_at").sort_index()

    catalog = station_catalog(df, stations)
    catalog.drop(columns="arch_order").to_csv(OUT_DIR / "resumen_estaciones.csv", index=False)
    profiles = station_profiles(df)
    medians = profiles.assign(weekend=(profiles["day_type"] == "fin_de_semana").astype(int)).set_index(["station_id", "weekend", "slot"])["median"]
    df = df.join(medians.rename("profile_median"), on=["station_id", "weekend", "slot"])
    df["ratio"] = df["demand"] / df["profile_median"]

    coefs, resid_full, resid_norain = fit_station_models(df)
    events = detect_events(context)
    findings["estaciones"] = {
        "arquetipos": catalog.groupby("archetype")["short"].apply(list).to_dict(),
        "demanda_media_red_15min": float(df["demand"].mean()),
        "r2_modelo_log_lineal_medio": float(coefs["r2"].mean()),
        "fin_de_semana_indice_medio": float(catalog["weekend_index"].mean()),
        "efecto_viernes_pct_medio": float(100 * (np.exp(coefs["is_friday_coef"]) - 1).mean()),
        "efecto_domingo_vs_sabado_pct_medio": float(100 * (np.exp(coefs["is_sunday_coef"]) - 1).mean()),
        "efecto_festivo_pct_medio": float(100 * (np.exp(coefs["is_holiday_coef"]) - 1).mean()),
        "temperatura_estaciones_significativas": int((coefs["temperature_c_p"] < 0.05).sum()),
    }

    fig_map(catalog)
    fig_distribution(df, catalog)
    findings["calendario"] = {"festivo_z_score_vs_laborales": fig_daily_series(df)}
    fig_hour_heatmap(df, catalog)
    fig_station_profiles(df, catalog)
    findings["estaciones"]["ari_regla_vs_clustering"] = fig_archetypes(df, catalog)
    fig_day_of_week(df, catalog)
    findings["contexto"] = fig_context(context_ts)
    fig_context_correlation(df, context_ts)
    findings["lluvia"] = fig_rain(df, coefs, catalog, resid_norain)
    findings["eventos"] = fig_events(df, context_ts, coefs, catalog, events)
    findings["autocorrelacion"] = fig_autocorrelation(df, catalog, resid_full)
    findings["correlacion_estaciones"] = fig_station_correlation(df, catalog, resid_full)
    findings["shock_diario"] = fig_daily_shock(df, catalog, resid_full)
    findings["ruido"] = fig_noise(df, resid_full)
    findings["estabilidad"] = fig_weekly_stability(df, catalog, coefs)
    decomposition = variance_decomposition(df)
    fig_variance(decomposition)
    findings["varianza_explicada_pct"] = dict(zip(decomposition["componente"], decomposition["aporte_pct"]))
    summary, per_station, by_hour = baselines(df)
    fig_baselines(summary, per_station, by_hour, catalog)
    findings["baselines_accuracy"] = dict(zip(summary["metodo"], summary["accuracy"]))
    findings["error_por_hora_top3"] = by_hour["error_share"].nlargest(3).round(4).to_dict()

    findings["dataset"] = {k: meta["dataset"][k] for k in ["dataset", "history_start", "history_end", "observation_rows"]}
    findings["dataset"]["api_version"] = meta["api_version"]
    findings["figuras"] = FIGURES
    (OUT_DIR / "findings.json").write_text(json.dumps(findings, indent=2, ensure_ascii=False, default=float))
    print(json.dumps(findings, indent=2, ensure_ascii=False, default=float))

    if not args.no_mlflow:
        run_id = log_to_mlflow(meta, findings, observations)
        print(f"\nMLflow run registrado: {run_id}")


if __name__ == "__main__":
    main()
