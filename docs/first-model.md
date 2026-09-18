# Primer modelo: perfil log-lineal

`LogLinearProfileModel` es el primer modelo reproducible del proyecto. Ajusta
un modelo independiente por estación sobre `log(demand)` con estas features:

- perfil `slot de 15 minutos × {laboral, fin de semana}`;
- `rain_forecast`, no `rain_mm`, porque el valor observado no se conoce para un
  horizonte futuro;
- `event_intensity`.

El EDA muestra que esas son las señales predictivas relevantes: el perfil
explica la mayor parte de la demanda, la lluvia tiene efecto por estación y el
evento eleva principalmente las estaciones de ocio. No se usa temperatura ni
rezagos: la primera repite la señal de la hora y los segundos añaden poco sobre
el perfil.

## Validación y métrica

La validación es temporal: se entrena hasta siete días antes del último slot y
se evalúan los últimos siete días. No se hace partición aleatoria.

La métrica oficial no agrupa primero las 12 estaciones. Para cada estación
calcula:

```text
WAPE_estación = sum(abs(real - predicción)) / sum(real)
accuracy_estación = 100 × max(0, 1 - WAPE_estación)
accuracy_final = promedio simple de las 12 accuracies
```

Esto evita que las estaciones de mayor volumen oculten un mal resultado en las
de menor volumen.

## Ejecutar

```bash
python -m pip install -e '.[ml]'
python examples/03_log_linear_baseline.py
```

En el snapshot inicial cargado en Supabase (26-jul a 8-sep-2026), la validación
de siete días alcanza **88,44** de accuracy promedio. El resultado usa solo
información disponible antes de predecir.

## Empaquetado y trazabilidad

Para crear una versión del modelo y su run de MLflow:

```bash
python examples/04_train_and_log_model.py
```

El comando genera `artifacts/models/<versión>/model.joblib` y, en el mismo
paquete, `manifest.json`, `metrics.json` y `training_result.json`. El run en el
experimento `pulso-transmi-models` registra el artefacto joblib, métricas globales
y por estación, parámetros, hashes SHA-256 del snapshot, rango de entrenamiento,
commit y estado dirty de Git, además del código y la documentación usados.

Para abrir el tracking local:

```bash
mlflow ui --backend-store-uri sqlite:///mlflow.db
```
