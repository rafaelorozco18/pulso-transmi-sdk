# Dashboard de monitoreo · Pulso TransMi

Bono de visualización del reto. Frontend en **Next.js** desplegado en **Vercel** que
muestra, en vivo, lo que el pipeline de GitHub Actions escribe en Supabase:

| Página | Qué muestra | Fuente |
|---|---|---|
| **Resumen** | accuracy oficial rolling 24 h y acumulada, puesto, cobertura de ciclos, modelo campeón, última ejecución de cada etapa, señales de drift y leaderboard (anónimo salvo nuestra fila) | `performance_snapshots`, `cycle_coverage`, `pipeline_stage_status`, `leaderboard` |
| **Drift** | matriz de drift por estación; concept drift (sesgo de nivel y forma horaria: mapas de calor, sesgo actual, mapa de estaciones); data drift (PSI y KS de la demanda, referencia vs. ventana actual por decil); drift de desempeño; alertas por corte y la respuesta del reentrenamiento | `drift_signals`, `retraining_decisions` |
| **Desempeño** | real vs. predicción enviada por estación y horizonte, accuracy por horizonte y por estación (producción vs. validación), distribución del error | `prediction_errors`, `observations_recent` |
| **Modelos y pipeline** | campeón vs. candidatos en cada reentrenamiento, historial de decisiones, versiones, salud de las etapas | `model_versions`, `retraining_decisions`, `pipeline_runs` |

## Seguridad

- El navegador nunca recibe credenciales: todas las consultas corren en el servidor
  (Server Components) y la página se renderiza en cada request.
- Se conecta con el rol **`dashboard_reader`** (migración
  [`20260925220000_dashboard_reader.sql`](../supabase/migrations/20260925220000_dashboard_reader.sql)):
  solo lectura, `statement_timeout` de 15 s y acceso únicamente al esquema
  `dashboard`, que son vistas curadas sin payloads, artefactos ni claves. No puede
  leer ni escribir `pulso`.
- La cadena de conexión vive en la variable cifrada `DASHBOARD_DATABASE_URL` de
  Vercel y en `.env` local; nunca en el repositorio.

## Desarrollo local

```bash
cd dashboard
npm install
grep ^DASHBOARD_DATABASE_URL= ../.env > .env.local
npm run dev        # http://localhost:3000
```

La página se actualiza sola cada minuto. El pipeline escribe una vez por ciclo
(~1 por hora real).

## Despliegue

Proyecto de Vercel `pulso-transmi-dashboard` con *Root Directory* `dashboard`.
`vercel.json` omite el build cuando un commit no toca esta carpeta, así que los
cambios del pipeline no redespliegan el dashboard. Despliegue manual desde la raíz
del repo: `vercel deploy --prod`.
