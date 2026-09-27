-- Vistas de solo lectura del tracking de MLflow para el dashboard: experimentos,
-- runs recientes y Model Registry. Las lee dashboard_reader; el esquema mlflow
-- sigue sin exponerse (las vistas corren con los privilegios de postgres).

create or replace view dashboard.mlflow_experiments as
select
    e.name,
    count(r.run_uuid) filter (where not exists (
        select 1 from mlflow.tags t where t.run_uuid = r.run_uuid and t.key = 'mlflow.parentRunId')) as runs,
    count(r.run_uuid) filter (where exists (
        select 1 from mlflow.tags t where t.run_uuid = r.run_uuid and t.key = 'mlflow.parentRunId')) as child_runs,
    to_timestamp(max(r.start_time) / 1000.0) as last_run_at
from mlflow.experiments e
left join mlflow.runs r on r.experiment_id = e.experiment_id and r.lifecycle_stage = 'active'
where e.lifecycle_stage = 'active' and e.name like 'pulso-transmi-%'
group by e.name;

-- Runs padre de reentrenamiento con sus métricas clave.
create or replace view dashboard.mlflow_retraining_runs as
select
    r.run_uuid as run_id,
    r.name as run_name,
    to_timestamp(r.start_time / 1000.0) as started_at,
    max(t.value) filter (where t.key = 'decision_id')::bigint as decision_id,
    max(t.value) filter (where t.key = 'trigger') as trigger,
    max(t.value) filter (where t.key = 'decision') as decision,
    max(m.value) filter (where m.key = 'live_accuracy_rolling_24h') as live_accuracy,
    max(m.value) filter (where m.key = 'champion_accuracy') as champion_accuracy,
    max(m.value) filter (where m.key = 'best_gain_vs_champion') as best_gain,
    (select count(*) from mlflow.tags c where c.key = 'mlflow.parentRunId' and c.value = r.run_uuid) as candidates
from mlflow.runs r
join mlflow.experiments e on e.experiment_id = r.experiment_id and e.name = 'pulso-transmi-retraining'
left join mlflow.tags t on t.run_uuid = r.run_uuid
left join mlflow.latest_metrics m on m.run_uuid = r.run_uuid
where r.lifecycle_stage = 'active'
  and not exists (select 1 from mlflow.tags p where p.run_uuid = r.run_uuid and p.key = 'mlflow.parentRunId')
group by r.run_uuid, r.name, r.start_time;

-- Model Registry: versiones, alias y versión de negocio.
create or replace view dashboard.mlflow_registry as
select
    v.name,
    v.version::int as version,
    to_timestamp(v.creation_time / 1000.0) as created_at,
    v.run_id,
    max(vt.value) filter (where vt.key = 'model_version') as model_version,
    coalesce(string_agg(distinct a.alias, ', '), '') as aliases
from mlflow.model_versions v
left join mlflow.model_version_tags vt on vt.name = v.name and vt.version = v.version
left join mlflow.registered_model_aliases a on a.name = v.name and a.version = v.version
group by v.name, v.version, v.creation_time, v.run_id;

grant select on dashboard.mlflow_experiments, dashboard.mlflow_retraining_runs, dashboard.mlflow_registry to dashboard_reader;
