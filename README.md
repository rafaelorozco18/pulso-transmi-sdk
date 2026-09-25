# Pulso TransMi — SDK para estudiantes

Starter kit oficial del reto MLOps **Pulso TransMi**. Incluye un cliente Python,
ejemplos reproducibles y una plantilla de GitHub Actions para construir un
pipeline que descargue datos, entrene, monitoree y posteriormente envíe
predicciones.

> **Disponible públicamente:** la API de lectura está en
> `https://pulso-transmi.72-60-245-2.sslip.io` y su documentación interactiva en
> [`/docs`](https://pulso-transmi.72-60-245-2.sslip.io/docs).

## El reto

Se pronostica demanda sintética cada 15 minutos para 12 estaciones reales de
TransMilenio. El sistema liberará observaciones con el tiempo y cambiará algunos
patrones durante la competencia. Un modelo entrenado una sola vez puede perder
desempeño: el objetivo es operar un pipeline capaz de medir, decidir y
reentrenar.

La demanda, clima y eventos son sintéticos. Los nombres y coordenadas de las
estaciones provienen de datos oficiales de TransMilenio.

## Inicio rápido

Requiere Python 3.11 o superior.

```bash
git clone https://github.com/uexternadojz/pulso-transmi-sdk.git
cd pulso-transmi-sdk
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[ml]'
cp .env.example .env
python examples/01_download.py
python examples/02_naive_baseline.py
python examples/03_log_linear_baseline.py
```

En Windows PowerShell, la activación es `.venv\Scripts\Activate.ps1`.

## Uso del SDK

```python
from pulso_transmi import PulsoTransmiClient

client = PulsoTransmiClient()

print(client.meta())
stations = client.stations()
observations = client.observations_dataframe(station_id="07107")
context = client.context_dataframe()

print(stations.head())
print(observations.tail())
```

El SDK recorre automáticamente todas las páginas. Si prefieres controlar cada
página, usa `client.observations_page(...)` y conserva `next_cursor` exactamente
como lo entrega la API.

## Datos iniciales

| Recurso | Tamaño |
|---|---:|
| Estaciones | 12 |
| Frecuencia | 15 minutos |
| Historia | 45 días |
| Periodos por estación | 4.320 |
| Observaciones | 51.840 |

Para evaluación local, usa una división temporal: por ejemplo, primeros 38 días
para entrenamiento y últimos 7 para validación. Una partición aleatoria mezcla
futuro y pasado y genera métricas engañosas.

## API de lectura `0.2.0`

| Método | Ruta | Uso |
|---|---|---|
| `GET` | `/health` | Estado básico |
| `GET` | `/v1/meta` | Versión, rango, hashes y enlaces |
| `GET` | `/v1/stations` | Catálogo geográfico |
| `GET` | `/v1/observations` | Demanda paginada |
| `GET` | `/v1/context` | Clima y eventos |
| `GET` | `/v1/downloads/{filename}` | Descarga completa |

Swagger está disponible en `/docs`. Consulta [docs/api.md](docs/api.md) para
filtros, paginación y errores.

## Estructura esperada del proyecto estudiantil

```text
mi-pulso-transmi/
├── src/
│   ├── ingest.py
│   ├── features.py
│   ├── train.py
│   ├── predict.py
│   └── monitor.py
├── tests/
├── artifacts/
├── requirements.txt o pyproject.toml
└── .github/workflows/pipeline.yml
```

El repositorio de cada equipo debe dejar trazabilidad de:

- cutoff de datos usado;
- versión o commit del código;
- features y modelo entrenado;
- métricas de validación temporal;
- momento y razón de cada reentrenamiento;
- errores de ingesta o inferencia.

## Pipeline MLOps automático

La API abre un ciclo por cada hora virtual en punto (una vez por hora real), lo deja abierto unos 25 minutos y
pide los 4 slots siguientes al `data_cutoff` (15, 30, 45 y 60 minutos) para las
12 estaciones. Un cron horario pierde la mayoría de ciclos, y el leaderboard
cuenta como error total cada ciclo no enviado. Por eso el pipeline corre como
un **vigilante** que sondea la API y, en cada ciclo nuevo, encadena cuatro
etapas idempotentes:

```text
API ──GET──▶ 1. colector ──▶ PostgreSQL (Supabase, esquema pulso)
                                │
     ┌──────────────────────────┘
     ▼
2. inferencia: modelo campeón (pulso.model_artifacts) ──POST /v1/submissions──▶ API
     │
     ▼ (la demanda real llega en los ciclos siguientes)
3. desempeño: accuracy oficial rolling, cobertura, drift, leaderboard
     │
     ▼ (programado cada 24 h virtuales o por alerta de drift)
4. reentrenamiento: recetas candidatas vs. campeón en ciclos simulados → promover o conservar
```

| Etapa | Archivo | Garantías |
|---|---|---|
| Colector | [`pipeline/collector.py`](pipeline/collector.py) | valida esquema, dominio y duplicados; upsert por `(station_id, observed_at)`; revisiones auditadas |
| Inferencia | [`pipeline/inference.py`](pipeline/inference.py) | una submission aceptada por ciclo; se persiste `pending` antes del POST y se reintenta con la misma `Idempotency-Key`; cada predicción guarda `model_version` y commit |
| Desempeño | [`pipeline/performance.py`](pipeline/performance.py) + [`drift.py`](pipeline/drift.py) | `performance_snapshots` (global, por estación y por horizonte) y leaderboard completo; `drift_signals`: desempeño (`wape_rolling`), concepto (`residual_bias`, `profile_shape`), datos (`demand_psi` con test KS) y calidad (`data_quality`) |
| Reentrenamiento | [`pipeline/retraining.py`](pipeline/retraining.py) | backtest sin fuga; solo promueve si el candidato supera al campeón por `min_gain`; cada decisión queda en `retraining_decisions` |
| Orquestador | [`pipeline/watch.py`](pipeline/watch.py) | sondea cada `poll_seconds`; un error no detiene el bucle; resumen en el job de Actions |

Las frecuencias, umbrales y recetas están en
[`pipeline/config.toml`](pipeline/config.toml), no en el código.
[`pipeline.yml`](.github/workflows/pipeline.yml) corre el vigilante unas 5,5 h
y luego se relanza a sí mismo con `workflow_dispatch`. El cron horario solo
reinicia la cadena si se corta. Requiere los secrets `DATABASE_URL` y
`PULSO_API_KEY`.

### Drift

Cada corte se evalúa con una ventana rolling de 24 h virtuales frente al campeón:

| Tipo | Señal | Qué mide | Alerta |
|---|---|---|---|
| Datos | `demand_psi` | PSI de log(demanda) frente a los últimos 14 días que vio el campeón, en las mismas horas y tipo de día; KS de dos muestras en `details` | PSI > 0,25 |
| Concepto | `residual_bias` | media de log(real / perfil) por estación: cambio de nivel | \|sesgo\| > 0,10 |
| Concepto | `profile_shape` | distancia de variación total entre la forma horaria real y la del perfil | > 0,10 |
| Desempeño | `wape_rolling` | métrica oficial sobre lo enviado | accuracy < 82 |
| Datos | `data_quality` | slots faltantes | cualquier hueco |

Dos o más estaciones en alerta en una señal por estación, o la accuracy bajo el
umbral, disparan la evaluación de reentrenamiento (con 6 h de cooldown). Los
umbrales se calibraron sobre el historial: el PSI de estaciones estables ronda
0,1 por ruido de muestreo y la distancia de forma, 0,04.
`python pipeline/backfill_drift.py` recalcula las señales de datos para cortes
anteriores a su introducción.

```bash
python pipeline/retraining.py        # crea el primer campeón (o --force para evaluar)
python pipeline/watch.py --once      # una pasada completa en local
python pipeline/sync_mlflow.py       # espeja modelos, decisiones y monitoreo en MLflow
mlflow ui --backend-store-uri sqlite:///mlflow.db
```

### Modelo en producción

`AdaptiveProfileForecaster` ([`forecasting.py`](src/pulso_transmi/forecasting.py))
combina el perfil log-lineal por estación con una corrección de nivel: la media
de `log(real / perfil)` en los últimos 4 slots, amortiguada por `0.8^h` según el
horizonte. La API no publica contexto después del corte inicial, así que para
predecir se usan lluvia y evento neutros; la corrección absorbe eventos, lluvia
y cambios de nivel. En los ciclos simulados sobre el stream de la competencia:

| Modelo | Accuracy |
|---|---:|
| Persistencia (último valor) | 73,79 |
| Perfil log-lineal (anterior) | 86,16 |
| Perfil + corrección de nivel | **86,90** |

La inferencia usa la misma función que el backtest (hay un test que lo
verifica), así que la accuracy de validación y la del leaderboard miden lo mismo.

### MLflow

Los runners de Actions son efímeros, así que Supabase es la fuente de verdad
de modelos (joblib en `pulso.model_artifacts`), decisiones y desempeño.
`sync_mlflow.py` lo replica en MLflow en tres experimentos
(`pulso-transmi-models`, `pulso-transmi-retraining` y
`pulso-transmi-monitoring`) y en el Model Registry `pulso-transmi-forecaster`,
donde el alias `champion` apunta al modelo activo.

## GitHub Actions

[`templates/pipeline.yml`](templates/pipeline.yml) es una plantilla manual. Cópiala
a `.github/workflows/pipeline.yml` dentro del repositorio de tu equipo. Cuando se
habilite la competencia, agrega el API key como secret y luego activa el horario
indicado por el profesor.

Nunca escribas API keys, contraseñas de Supabase ni tokens dentro del código.

## Supabase y Vercel

Supabase es opcional para persistir ejecuciones, métricas, predicciones y estado
del modelo. Vercel es opcional y corresponde al bono de visualización. Ninguna de
las dos plataformas reemplaza el repositorio ni GitHub Actions.

Consulta [docs/student-project.md](docs/student-project.md) para el flujo completo
y los entregables.

### Dashboard de monitoreo (bono)

**https://pulso-transmi-dashboard-lilac.vercel.app** · código en [`dashboard/`](dashboard/)

Next.js en Vercel que lee en vivo lo que el pipeline escribe en Supabase: drift
por estación (sesgo de nivel, WAPE rolling, calidad de datos) y la respuesta del
reentrenamiento, accuracy oficial y puesto en el leaderboard, real vs. predicción,
cobertura de ciclos, versiones del modelo y salud de cada etapa. Consulta el
servidor con el rol de solo lectura `dashboard_reader`, que solo ve vistas curadas
del esquema `dashboard`; ninguna clave llega al navegador.

## Métrica

La referencia actual es:

```text
WAPE = sum(abs(real - predicción)) / sum(real)
Accuracy = 100 × max(0, 1 - WAPE)
```

La métrica se calcula por estación y luego se promedia. El contrato definitivo
de submissions y leaderboard se publicará antes de iniciar la ventana competitiva.

## Primer modelo

El primer modelo reproducible es una regresión log-lineal por estación: perfil
horario, tipo de día, lluvia pronosticada y evento. En la validación temporal de
los últimos 7 días del corte inicial alcanza **88,44** de accuracy promedio con
la métrica oficial (WAPE por estación y promedio simple entre las 12). Consulta
[docs/first-model.md](docs/first-model.md) y ejecuta
`python examples/03_log_linear_baseline.py`.

Para empaquetarlo como `joblib` y dejar un run versionado en MLflow, ejecuta
`python examples/04_train_and_log_model.py`.

## Desarrollo del SDK

```bash
python -m pip install -e '.[dev,ml]'
pytest -q
```

Este repositorio es público para estudiantes. No debe contener ground truth
futuro, semillas, configuración privada del escenario ni parámetros de drift.
