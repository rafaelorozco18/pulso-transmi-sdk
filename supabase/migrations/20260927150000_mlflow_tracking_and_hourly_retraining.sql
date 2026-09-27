-- MLflow en la nube y reentrenamiento horario.
--
-- MLflow guarda su tracking y su Model Registry en el esquema `mlflow` (sus
-- tablas las crea y migra el propio MLflow al conectarse). Los artefactos van a
-- Supabase Storage, bucket privado `mlflow`. El esquema no se expone en la API.
create schema if not exists mlflow;
revoke all on schema mlflow from public;

-- Nueva etapa del pipeline: espejo en MLflow (pipeline/tracking.py).
alter table pulso.pipeline_runs drop constraint pipeline_runs_stage_check;
alter table pulso.pipeline_runs add constraint pipeline_runs_stage_check
    check (stage in ('collector', 'inference', 'performance', 'retraining', 'tracking'));

-- Trazabilidad decisión → run de MLflow.
alter table pulso.retraining_decisions add column if not exists mlflow_run_id text;

-- Los run ids apuntaban a un mlflow.db local; se re-versionan en el tracking en la nube.
update pulso.model_versions set mlflow_run_id = 'unsynced', mlflow_model_uri = null;
