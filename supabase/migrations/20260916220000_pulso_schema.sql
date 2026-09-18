-- =============================================================================
-- Pulso TransMi · esquema PostgreSQL (compatible con Supabase)
--
-- Diseño derivado del EDA (EDA/report/index.html) y del contrato de la API 0.3.0:
--   * La grilla es fija: 12 estaciones × slots de 15 min. Clave natural
--     (station_id, observed_at); los IDs son texto de 5 dígitos con ceros.
--   * El contexto (lluvia, temperatura, evento) es único para toda la red:
--     una fila por timestamp, no por estación.
--   * La demanda se explica casi toda con estación × tipo de día × slot
--     (97 % de la varianza log). Lunes–viernes son equivalentes y sábado ≈
--     domingo, así que el perfil base se guarda por day_type, no por día de
--     la semana. Ese perfil es la referencia para medir drift.
--   * Los festivos no mostraron efecto en la historia, pero se registran en el
--     calendario para vigilar si el escenario cambia.
--   * Trazabilidad exigida: cutoff, commit, versión de modelo, métricas,
--     razones de reentrenamiento y errores de ingesta o inferencia.
--   * MLflow guarda runs y artefactos. Aquí solo se referencia mlflow_run_id.
--
-- Convenciones: timestamptz en todo (se guarda en UTC y se presenta en
-- America/Bogota); estados como text + CHECK (más fácil de migrar que ENUM).
-- =============================================================================

create schema if not exists pulso;
set search_path = pulso, public;

-- -----------------------------------------------------------------------------
-- Utilidades
-- -----------------------------------------------------------------------------

-- Slot 0..95 del día local de Bogotá (sin horario de verano: UTC-5 fijo).
create or replace function pulso.slot_of(ts timestamptz)
returns smallint
language sql
immutable
parallel safe
set search_path = ''
as $$
  select ((extract(epoch from (ts at time zone 'America/Bogota')::time) / 900))::smallint
$$;

create or replace function pulso.day_type_of(ts timestamptz)
returns text
language sql
immutable
parallel safe
set search_path = ''
as $$
  select case when extract(isodow from ts at time zone 'America/Bogota') >= 6
              then 'fin_de_semana' else 'laboral' end
$$;

-- -----------------------------------------------------------------------------
-- Catálogos
-- -----------------------------------------------------------------------------

create table pulso.stations (
    station_id    text primary key check (station_id ~ '^[0-9]{5}$'),
    station_name  text not null,
    corridor      text not null,
    latitude      numeric(9, 6) not null check (latitude between -90 and 90),
    longitude     numeric(9, 6) not null check (longitude between -180 and 180),
    -- Arquetipo por forma del perfil horario (EDA, figura 06). Sirve como
    -- feature categórica y para agrupar alertas.
    archetype     text check (archetype in (
                      'portal_origen_am', 'intercambio_doble_pico', 'oficinas_destino_am',
                      'universitaria_mediodia', 'ocio_nocturno')),
    created_at    timestamptz not null default now()
);

create table pulso.calendar_days (
    day           date primary key,
    day_type      text not null check (day_type in ('laboral', 'fin_de_semana')),
    is_holiday    boolean not null default false,
    holiday_name  text,
    check (is_holiday = (holiday_name is not null))
);

-- -----------------------------------------------------------------------------
-- Ingesta
-- -----------------------------------------------------------------------------

create table pulso.dataset_snapshots (
    snapshot_id    bigint generated always as identity primary key,
    dataset        text not null,                 -- p. ej. pulso-transmi-starter-v1
    api_version    text not null,
    history_start  timestamptz not null,
    history_end    timestamptz not null,
    files          jsonb not null,                -- {archivo: {rows, sha256}}
    fetched_at     timestamptz not null default now(),
    unique (dataset, history_end, api_version),
    check (history_end > history_start)
);

create table pulso.ingestion_runs (
    run_id           bigint generated always as identity primary key,
    source           text not null check (source in (
                         'bootstrap_download', 'observations', 'stream_observations', 'context')),
    status           text not null default 'running' check (status in ('running', 'success', 'failed')),
    started_at       timestamptz not null default now(),
    finished_at      timestamptz,
    cursor_in        text,
    cursor_out       text,
    max_observed_at  timestamptz,
    rows_received    integer not null default 0 check (rows_received >= 0),
    rows_inserted    integer not null default 0 check (rows_inserted >= 0),
    rows_updated     integer not null default 0 check (rows_updated >= 0),
    api_version      text,
    server_time      timestamptz,
    snapshot_id      bigint references pulso.dataset_snapshots (snapshot_id),
    git_commit       text check (git_commit ~ '^[0-9a-f]{7,40}$'),
    github_run_id    text,
    error_message    text,
    check (finished_at is null or finished_at >= started_at),
    check (status <> 'failed' or error_message is not null)
);

create index ingestion_runs_source_started_idx on pulso.ingestion_runs (source, started_at desc);

-- Estado incremental: el cursor exacto que devolvió la API, sin modificar.
create table pulso.ingestion_cursors (
    source            text primary key check (source in ('observations', 'stream_observations', 'context')),
    cursor            text,
    last_observed_at  timestamptz,
    last_run_id       bigint references pulso.ingestion_runs (run_id),
    updated_at        timestamptz not null default now()
);

create table pulso.observations (
    station_id        text not null references pulso.stations (station_id),
    observed_at       timestamptz not null check ((extract(epoch from observed_at)::bigint % 900) = 0),
    demand            integer not null check (demand >= 0),
    ingestion_run_id  bigint references pulso.ingestion_runs (run_id),
    ingested_at       timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    primary key (station_id, observed_at)
);

-- Consultas de red completa por ventana temporal (monitoreo, features de contexto).
create index observations_observed_at_idx on pulso.observations (observed_at);

-- Si la API reenvía un valor distinto para un slot ya guardado, queda auditado.
create table pulso.observation_revisions (
    station_id        text not null,
    observed_at       timestamptz not null,
    previous_demand   integer not null,
    new_demand        integer not null,
    ingestion_run_id  bigint references pulso.ingestion_runs (run_id),
    revised_at        timestamptz not null default now(),
    foreign key (station_id, observed_at) references pulso.observations (station_id, observed_at)
);

create or replace function pulso.track_observation_revision()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.demand is distinct from old.demand then
    insert into pulso.observation_revisions (station_id, observed_at, previous_demand, new_demand, ingestion_run_id)
    values (old.station_id, old.observed_at, old.demand, new.demand, new.ingestion_run_id);
    new.updated_at := now();
  end if;
  return new;
end;
$$;

create trigger observations_revision_trg
before update on pulso.observations
for each row execute function pulso.track_observation_revision();

create table pulso.context (
    observed_at           timestamptz primary key check ((extract(epoch from observed_at)::bigint % 900) = 0),
    rain_mm               double precision not null check (rain_mm >= 0),
    rain_forecast         double precision not null check (rain_forecast >= 0),
    temperature_c         double precision not null,
    temperature_forecast  double precision not null,
    -- Pulsos gaussianos en [0, 1]; sus colas llegan a 1e-190, por eso double precision.
    event_intensity       double precision not null check (event_intensity between 0 and 1),
    ingestion_run_id      bigint references pulso.ingestion_runs (run_id),
    ingested_at           timestamptz not null default now()
);

-- -----------------------------------------------------------------------------
-- Perfil base (referencia para features y drift)
-- -----------------------------------------------------------------------------

create table pulso.profile_versions (
    profile_version_id    bigint generated always as identity primary key,
    data_start            timestamptz not null,
    data_end              timestamptz not null,
    event_free_threshold  double precision not null default 0.01,
    mlflow_run_id         text,
    created_at            timestamptz not null default now(),
    check (data_end > data_start)
);

create table pulso.station_profiles (
    profile_version_id  bigint not null references pulso.profile_versions (profile_version_id) on delete cascade,
    station_id          text not null references pulso.stations (station_id),
    day_type            text not null check (day_type in ('laboral', 'fin_de_semana')),
    slot                smallint not null check (slot between 0 and 95),
    median              double precision not null check (median >= 0),
    mean                double precision not null check (mean >= 0),
    p10                 double precision not null,
    p90                 double precision not null,
    n                   integer not null check (n > 0),
    primary key (profile_version_id, station_id, day_type, slot),
    check (p10 <= median and median <= p90)
);

-- -----------------------------------------------------------------------------
-- Modelos y decisiones
-- -----------------------------------------------------------------------------

create table pulso.model_versions (
    model_version       text primary key check (char_length(model_version) between 1 and 64),
    mlflow_run_id       text not null,
    mlflow_model_uri    text,
    git_commit          text not null check (git_commit ~ '^[0-9a-f]{7,40}$'),
    trained_at          timestamptz not null,
    training_data_end   timestamptz not null,           -- = model.training_data_end del submission
    profile_version_id  bigint references pulso.profile_versions (profile_version_id),
    feature_set         jsonb not null,
    hyperparameters     jsonb not null default '{}'::jsonb,
    validation_metrics  jsonb not null default '{}'::jsonb,  -- accuracy global, por estación y por horizonte
    status              text not null default 'candidate' check (status in ('candidate', 'active', 'retired', 'rejected')),
    promoted_at         timestamptz,
    retired_at          timestamptz,
    created_at          timestamptz not null default now(),
    check (training_data_end <= trained_at)
);

-- Solo un modelo activo a la vez.
create unique index model_versions_single_active_idx on pulso.model_versions ((true)) where status = 'active';

create table pulso.retraining_decisions (
    decision_id              bigint generated always as identity primary key,
    evaluated_at             timestamptz not null default now(),
    active_model_version     text references pulso.model_versions (model_version),
    candidate_model_version  text references pulso.model_versions (model_version),
    trigger                  text not null check (trigger in ('scheduled', 'drift', 'performance', 'manual', 'initial')),
    decision                 text not null check (decision in ('keep', 'retrain', 'promote', 'rollback')),
    signals                  jsonb not null default '{}'::jsonb,   -- snapshot de drift_signals que motivó la decisión
    reason                   text not null,
    git_commit               text check (git_commit ~ '^[0-9a-f]{7,40}$'),
    github_run_id            text
);

-- -----------------------------------------------------------------------------
-- Ciclos, predicciones y envíos (contrato POST /v1/submissions, schema 1.0)
-- -----------------------------------------------------------------------------

create table pulso.forecast_cycles (
    cycle_id      text primary key check (cycle_id ~ '^cyc_[A-Za-z0-9_-]{1,80}$'),
    opens_at      timestamptz,
    closes_at     timestamptz,
    data_cutoff   timestamptz,
    target_times  timestamptz[],                    -- horizontes solicitados por el ciclo
    raw           jsonb not null,                   -- respuesta completa de /v1/forecast-cycles/current
    fetched_at    timestamptz not null default now()
);

create table pulso.forecast_runs (
    client_run_id   text primary key check (char_length(client_run_id) between 1 and 128),
    cycle_id        text references pulso.forecast_cycles (cycle_id),
    model_version   text not null references pulso.model_versions (model_version),
    data_cutoff     timestamptz not null,
    status          text not null default 'running' check (status in ('running', 'success', 'failed')),
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    git_commit      text not null check (git_commit ~ '^[0-9a-f]{7,40}$'),
    github_run_id   text,
    error_message   text,
    check (status <> 'failed' or error_message is not null)
);

create index forecast_runs_cycle_idx on pulso.forecast_runs (cycle_id);

create table pulso.predictions (
    client_run_id    text not null references pulso.forecast_runs (client_run_id) on delete cascade,
    station_id       text not null references pulso.stations (station_id),
    target_at        timestamptz not null check ((extract(epoch from target_at)::bigint % 900) = 0),
    horizon_steps    smallint not null check (horizon_steps > 0),   -- (target_at − data_cutoff) / 15 min
    value            double precision not null check (value between 0 and 100000),
    -- Intervalo opcional: el ruido es multiplicativo (CV ≈ 15 %), útil para el dashboard.
    lower_bound      double precision check (lower_bound >= 0),
    upper_bound      double precision,
    primary key (client_run_id, station_id, target_at),
    check (lower_bound is null or upper_bound is null or lower_bound <= upper_bound)
);

create index predictions_target_idx on pulso.predictions (station_id, target_at);

create table pulso.submissions (
    submission_pk      bigint generated always as identity primary key,
    client_run_id      text not null references pulso.forecast_runs (client_run_id),
    idempotency_key    text not null unique check (char_length(idempotency_key) between 8 and 128),
    attempt            smallint not null default 1 check (attempt >= 1),
    payload            jsonb not null,                -- cuerpo exacto enviado (≤ 100 predicciones)
    submitted_at       timestamptz not null default now(),
    http_status        smallint,
    api_submission_id  text unique,
    status             text not null default 'pending' check (status in ('pending', 'accepted', 'rejected', 'error')),
    response           jsonb,
    error_message      text
);

create index submissions_run_idx on pulso.submissions (client_run_id);

create table pulso.leaderboard_snapshots (
    fetched_at  timestamptz not null default now(),
    window_name text not null check (window_name in ('cumulative', 'rolling_24h')),
    payload     jsonb not null,
    primary key (window_name, fetched_at)
);

-- -----------------------------------------------------------------------------
-- Monitoreo
-- -----------------------------------------------------------------------------

create table pulso.drift_signals (
    signal_id           bigint generated always as identity primary key,
    computed_at         timestamptz not null default now(),
    window_start        timestamptz not null,
    window_end          timestamptz not null,
    station_id          text references pulso.stations (station_id),   -- null = señal de red
    signal              text not null check (signal in (
                            'level_vs_profile',      -- media geométrica de demanda / mediana del perfil
                            'wape_rolling',          -- desempeño del modelo activo
                            'residual_bias',         -- sesgo medio del log-residuo
                            'rain_sensitivity',      -- elasticidad a la lluvia re-estimada
                            'event_sensitivity',     -- lift de evento re-estimado
                            'profile_shape',         -- distancia entre forma horaria reciente e histórica
                            'context_distribution',  -- cambio en lluvia / temperatura / eventos
                            'data_quality')),        -- huecos, duplicados, valores fuera de rango
    value               double precision not null,
    threshold           double precision,
    is_alert            boolean not null default false,
    model_version       text references pulso.model_versions (model_version),
    profile_version_id  bigint references pulso.profile_versions (profile_version_id),
    details             jsonb not null default '{}'::jsonb,
    check (window_end > window_start)
);

create index drift_signals_lookup_idx on pulso.drift_signals (signal, station_id, computed_at desc);

-- -----------------------------------------------------------------------------
-- Vistas para métricas y dashboard
-- -----------------------------------------------------------------------------

-- Error de cada predicción cuya observación ya llegó.
create view pulso.v_prediction_errors
with (security_invoker = true) as
select
    p.client_run_id,
    r.model_version,
    r.cycle_id,
    p.station_id,
    p.target_at,
    p.horizon_steps,
    p.value              as predicted,
    o.demand             as actual,
    abs(o.demand - p.value) as abs_error
from pulso.predictions p
join pulso.forecast_runs r using (client_run_id)
join pulso.observations o on o.station_id = p.station_id and o.observed_at = p.target_at;

-- Métrica oficial: WAPE por estación y luego promedio simple entre estaciones.
create view pulso.v_accuracy_by_station
with (security_invoker = true) as
select
    model_version,
    station_id,
    horizon_steps,
    count(*)                                        as n,
    sum(abs_error) / nullif(sum(actual), 0)         as wape,
    100 * greatest(0, 1 - sum(abs_error) / nullif(sum(actual), 0)) as accuracy
from pulso.v_prediction_errors
group by model_version, station_id, horizon_steps;

create view pulso.v_accuracy_rolling_24h
with (security_invoker = true) as
with recent as (
    select *
    from pulso.v_prediction_errors
    where target_at > (select max(observed_at) from pulso.observations) - interval '24 hours'
), per_station as (
    select model_version, station_id,
           100 * greatest(0, 1 - sum(abs_error) / nullif(sum(actual), 0)) as accuracy
    from recent
    group by model_version, station_id
)
select model_version, avg(accuracy) as accuracy, count(*) as stations
from per_station
group by model_version;

-- Demanda observada frente al perfil base: la señal principal de drift del EDA.
create view pulso.v_observations_vs_profile
with (security_invoker = true) as
select
    o.station_id,
    o.observed_at,
    o.demand,
    sp.profile_version_id,
    sp.median                         as profile_median,
    o.demand / nullif(sp.median, 0)   as ratio,
    c.rain_mm,
    c.event_intensity
from pulso.observations o
join pulso.station_profiles sp
  on sp.station_id = o.station_id
 and sp.day_type = pulso.day_type_of(o.observed_at)
 and sp.slot = pulso.slot_of(o.observed_at)
left join pulso.context c on c.observed_at = o.observed_at;

create view pulso.v_pipeline_status
with (security_invoker = true) as
select distinct on (source)
    source, status, started_at, finished_at, max_observed_at, rows_inserted, error_message
from pulso.ingestion_runs
order by source, started_at desc;

-- -----------------------------------------------------------------------------
-- Supabase: permisos. Un esquema propio no hereda los grants de public.
-- Solo service_role (backend / GitHub Actions) opera sobre pulso; anon y
-- authenticated no tienen acceso hasta definir políticas para el dashboard.
-- -----------------------------------------------------------------------------

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant usage on schema pulso to service_role;
    grant all on all tables in schema pulso to service_role;
    grant all on all sequences in schema pulso to service_role;
    grant execute on all functions in schema pulso to service_role;
    alter default privileges in schema pulso grant all on tables to service_role;
    alter default privileges in schema pulso grant all on sequences to service_role;
    alter default privileges in schema pulso grant execute on functions to service_role;
  end if;
end
$$;

revoke all on schema pulso from public;
revoke execute on all functions in schema pulso from public;

-- -----------------------------------------------------------------------------
-- Supabase: RLS activo sin políticas públicas. GitHub Actions usa la
-- service_role key (salta RLS); el dashboard debe leer mediante políticas o
-- funciones explícitas, nunca con la service_role en el navegador.
-- -----------------------------------------------------------------------------

alter table pulso.stations              enable row level security;
alter table pulso.calendar_days         enable row level security;
alter table pulso.dataset_snapshots     enable row level security;
alter table pulso.ingestion_runs        enable row level security;
alter table pulso.ingestion_cursors     enable row level security;
alter table pulso.observations          enable row level security;
alter table pulso.observation_revisions enable row level security;
alter table pulso.context               enable row level security;
alter table pulso.profile_versions      enable row level security;
alter table pulso.station_profiles      enable row level security;
alter table pulso.model_versions        enable row level security;
alter table pulso.retraining_decisions  enable row level security;
alter table pulso.forecast_cycles       enable row level security;
alter table pulso.forecast_runs         enable row level security;
alter table pulso.predictions           enable row level security;
alter table pulso.submissions           enable row level security;
alter table pulso.leaderboard_snapshots enable row level security;
alter table pulso.drift_signals         enable row level security;
