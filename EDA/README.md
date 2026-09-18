# EDA · Pulso TransMi

Análisis exploratorio profundo del corte inicial (26 jul – 8 sep 2026, 12
estaciones × 4.320 slots de 15 min). El reporte completo, con las 18 figuras y
su interpretación, está en [`report/index.html`](report/index.html).

## Contenido

| Ruta | Qué es |
|---|---|
| [`eda.py`](eda.py) | Pipeline reproducible: descarga con el SDK (verifica SHA-256), integridad, modelos, figuras, tablas y run de MLflow |
| [`report/index.html`](report/index.html) | Reporte con hallazgos, figuras e implicaciones para el modelo |
| [`report/figures/`](report/figures/) | Figuras PNG (`01_…` a `18_…`) |
| [`outputs/`](outputs/) | Tablas CSV y `findings.json` con todas las métricas |
| [`schema.sql`](schema.sql) | Esquema PostgreSQL / Supabase derivado del EDA y del contrato de la API 0.3.0 |
| `data/` | Copia local del corte (ignorada por git) |

## Ejecutar

Desde la raíz del repositorio:

```bash
python -m pip install -e '.[ml]' -r EDA/requirements.txt
python EDA/eda.py               # usa EDA/data si existe
python EDA/eda.py --refresh     # vuelve a descargar desde la API
python EDA/eda.py --no-mlflow   # sin registrar el run
```

Si la API no responde y ya hay datos locales, el script sigue con esa copia
después de verificar sus hashes contra el `metadata.json` guardado.

## MLflow

Cada ejecución registra un run en el experimento `pulso-transmi-eda` con los
parámetros del corte (versión de API, rango, SHA-256), el dataset como input,
las métricas de `findings.json` y como artefactos las figuras, tablas, esquema y
reporte.

```bash
mlflow ui --backend-store-uri sqlite:///mlflow.db
```

Por defecto usa `sqlite:///mlflow.db` en la raíz del repo. Define
`MLFLOW_TRACKING_URI` para usar un servidor compartido.

## Esquema Postgres

Desplegado en Supabase, proyecto `pulso-transmi` (ref `hsebqvgmowwqgqtxgbfx`,
us-east-1). Los cambios se versionan como migraciones en
[`supabase/migrations/`](../supabase/migrations/):

```bash
supabase link --project-ref hsebqvgmowwqgqtxgbfx
supabase migration new <cambio>   # nunca editar una migración ya aplicada
supabase db push
```

[`schema.sql`](schema.sql) es la foto completa equivalente, útil para un
Postgres local (`psql "$DATABASE_URL" -f EDA/schema.sql`). Las credenciales
(`SUPABASE_DB_PASSWORD`, `DATABASE_URL`) viven solo en `.env`; en GitHub Actions
van como secrets.

Crea el esquema `pulso` con 18 tablas y 5 vistas: datos de la API, ingesta
incremental con cursores, perfiles base, versiones de modelo, ciclos,
predicciones, envíos idempotentes y señales de drift. Se probó en PostgreSQL 16 local
cargando las 51.840 observaciones reales y está aplicado en Supabase (Postgres 17.6).

## Hallazgos principales

1. Estación × tipo de día × slot explica el 96,8 % de la varianza de `log(demanda)`.
2. Cinco arquetipos horarios: portal (AM), intercambio (doble pico), oficinas (PM), universitaria (mediodía) y ocio nocturno.
3. Solo hay dos tipos de día (laboral / fin de semana). Los festivos no tuvieron efecto.
4. La lluvia actúa en el mismo intervalo: −2,7 % a −9,8 % por mm en 10 estaciones, +3,6 % y +5,9 % en las de ocio.
5. Eventos: 5 pulsos gaussianos a las 19:45 con +38–40 % en ocio y +8–12 % en el resto.
6. La temperatura no aporta nada: es colineal con la hora.
7. Ruido multiplicativo (CV ≈ 15 %) y casi sin memoria. El techo práctico de accuracy es ~88,7.
8. Mejor baseline factible: perfil + lluvia pronosticada + evento = 88,44 (perfil solo: 87,94).
9. Deriva lenta positiva en 7 estaciones: hay que vigilar el nivel frente al perfil.
