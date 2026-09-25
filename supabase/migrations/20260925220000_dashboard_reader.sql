-- Dashboard de monitoreo (bono de visualización, desplegado en Vercel).
--
-- El dashboard no toca el esquema pulso: lee solo el esquema `dashboard`, un
-- conjunto de vistas curadas sin payloads, artefactos ni claves de idempotencia.
-- Las vistas no son security_invoker: se ejecutan con los privilegios de su
-- dueño (postgres, dueño de las tablas), así que el rol del dashboard no
-- necesita acceso ni políticas RLS sobre pulso.
--
-- Rol `dashboard_reader`: solo lectura, sin acceso a pulso. Su contraseña NO se
-- versiona; se asigna aparte:
--   alter role dashboard_reader with password '<secreto>';
-- y la cadena de conexión vive solo en .env y en las variables de Vercel.
-- El esquema no se expone en la API REST de Supabase (config.toml).

create schema if not exists dashboard;
revoke all on schema dashboard from public;

-- Catálogo de estaciones (mapa).
create or replace view dashboard.stations as
select station_id, station_name, corridor, latitude::double precision as latitude,
       longitude::double precision as longitude, archetype
from pulso.stations;

-- Señales de drift tal como las guarda la etapa de desempeño.
create or replace view dashboard.drift_signals as
select signal_id, computed_at, window_start, window_end, station_id, signal, value,
       threshold, is_alert, model_version, details
from pulso.drift_signals;

-- Accuracy rolling, cobertura y leaderboard por corte evaluado.
create or replace view dashboard.performance_snapshots as
select snapshot_id, computed_at, data_cutoff, window_hours, model_version, accuracy,
       cycles_submitted, cycles_expected, n_predictions, by_station, by_horizon,
       leaderboard -> 'cumulative'  as leaderboard_cumulative,
       leaderboard -> 'rolling_24h' as leaderboard_rolling_24h
from pulso.performance_snapshots;

-- Última ejecución de cada etapa del pipeline.
create or replace view dashboard.pipeline_stage_status as
select distinct on (stage)
    stage, status, started_at, finished_at,
    extract(epoch from finished_at - started_at)::double precision as duration_s,
    cycle_id, details, left(error_message, 500) as error_message, git_commit, github_run_id
from pulso.pipeline_runs
order by stage, started_at desc;

-- Historial de ejecuciones (sin detalles pesados).
create or replace view dashboard.pipeline_runs as
select run_id, stage, status, started_at, finished_at,
       extract(epoch from finished_at - started_at)::double precision as duration_s,
       cycle_id, left(error_message, 500) as error_message, github_run_id
from pulso.pipeline_runs;

-- Cobertura: cada ciclo oficial conocido y si tuvo submission aceptada.
create or replace view dashboard.cycle_coverage as
select
    c.cycle_id, c.data_cutoff, c.opens_at, c.closes_at,
    coalesce(bool_or(s.status = 'accepted'), false) as accepted,
    max(s.submitted_at) filter (where s.status = 'accepted') as accepted_at,
    max(s.attempt) as attempts,
    max(r.model_version) as model_version
from pulso.forecast_cycles c
left join pulso.forecast_runs r on r.cycle_id = c.cycle_id
left join pulso.submissions s on s.client_run_id = r.client_run_id
group by c.cycle_id, c.data_cutoff, c.opens_at, c.closes_at;

-- Registro de modelos (sin el joblib).
create or replace view dashboard.model_versions as
select model_version, git_commit, trained_at, training_data_end, hyperparameters,
       validation_metrics, status, promoted_at, retired_at, created_at
from pulso.model_versions;

create or replace view dashboard.retraining_decisions as
select decision_id, evaluated_at, trigger, decision, reason, active_model_version,
       candidate_model_version, signals, git_commit, github_run_id
from pulso.retraining_decisions;

-- Predicción enviada (submission aceptada) frente a la demanda real, cuando ya
-- llegó. Si un ciclo tuvo varias submissions aceptadas, cuenta la última.
create or replace view dashboard.prediction_errors as
select distinct on (r.cycle_id, p.station_id, p.target_at)
    r.cycle_id, r.model_version, r.data_cutoff, p.station_id, p.target_at, p.horizon_steps,
    p.value as predicted, p.lower_bound, p.upper_bound, o.demand::double precision as actual,
    abs(o.demand - p.value) as abs_error
from pulso.predictions p
join pulso.forecast_runs r using (client_run_id)
join pulso.submissions s using (client_run_id)
join pulso.observations o on o.station_id = p.station_id and o.observed_at = p.target_at
where s.status = 'accepted'
order by r.cycle_id, p.station_id, p.target_at, s.submitted_at desc;

-- Demanda observada de los últimos 7 días virtuales.
create or replace view dashboard.observations_recent as
select station_id, observed_at, demand::double precision as demand
from pulso.observations
where observed_at > (select max(observed_at) from pulso.observations) - interval '7 days';

-- Reloj de datos: último corte observado y cuándo se ingirió.
create or replace view dashboard.data_clock as
select
    (select max(observed_at) from pulso.observations) as max_observed_at,
    (select count(*) from pulso.observations) as observations,
    (select max(finished_at) from pulso.ingestion_runs where status = 'success') as last_ingestion_at;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'dashboard_reader') then
    create role dashboard_reader with login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
  end if;
end
$$;

alter role dashboard_reader set default_transaction_read_only = on;
alter role dashboard_reader set statement_timeout = '15s';
alter role dashboard_reader set search_path = dashboard;

grant usage on schema dashboard to dashboard_reader;
grant select on all tables in schema dashboard to dashboard_reader;
alter default privileges in schema dashboard grant select on tables to dashboard_reader;
