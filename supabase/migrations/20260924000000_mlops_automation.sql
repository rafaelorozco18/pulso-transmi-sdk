-- Automatización MLOps: registro de modelos servibles, desempeño y bitácora.
--
-- Los runners de GitHub Actions son efímeros: el joblib del modelo campeón vive
-- aquí (bytea) para que cada ejecución cargue exactamente la versión activa.

create table if not exists pulso.model_artifacts (
    model_version  text primary key references pulso.model_versions (model_version) on delete cascade,
    artifact       bytea not null,
    sha256         text not null check (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes     integer not null check (size_bytes > 0),
    created_at     timestamptz not null default now()
);

-- Desempeño medido tras cada ciclo: predicciones propias vs. demanda real.
create table if not exists pulso.performance_snapshots (
    snapshot_id          bigint generated always as identity primary key,
    computed_at          timestamptz not null default now(),
    data_cutoff          timestamptz not null,
    window_hours         integer not null check (window_hours > 0),
    model_version        text references pulso.model_versions (model_version),
    accuracy             double precision,
    cycles_submitted     integer not null default 0,
    cycles_expected      integer not null default 0,
    n_predictions        integer not null default 0,
    by_station           jsonb not null default '{}'::jsonb,
    by_horizon           jsonb not null default '{}'::jsonb,
    leaderboard          jsonb not null default '{}'::jsonb
);

create index if not exists performance_snapshots_cutoff_idx on pulso.performance_snapshots (data_cutoff desc);
create index if not exists performance_snapshots_model_idx on pulso.performance_snapshots (model_version);

-- Una fila por etapa ejecutada (colector, inferencia, desempeño, reentrenamiento).
create table if not exists pulso.pipeline_runs (
    run_id          bigint generated always as identity primary key,
    stage           text not null check (stage in ('collector', 'inference', 'performance', 'retraining')),
    status          text not null default 'running' check (status in ('running', 'success', 'skipped', 'failed')),
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    cycle_id        text,
    details         jsonb not null default '{}'::jsonb,
    error_message   text,
    git_commit      text,
    github_run_id   text,
    check (status <> 'failed' or error_message is not null)
);

create index if not exists pipeline_runs_stage_idx on pulso.pipeline_runs (stage, started_at desc);

-- Última ejecución de cada etapa (observabilidad rápida).
create or replace view pulso.v_pipeline_stage_status
with (security_invoker = true) as
select distinct on (stage)
    stage, status, started_at, finished_at,
    extract(epoch from finished_at - started_at) as duration_s,
    cycle_id, details, error_message
from pulso.pipeline_runs
order by stage, started_at desc;

-- Ciclos oficiales conocidos frente a submissions aceptadas (cobertura).
create or replace view pulso.v_cycle_coverage
with (security_invoker = true) as
select
    c.cycle_id,
    c.data_cutoff,
    c.closes_at,
    bool_or(s.status = 'accepted') as accepted,
    max(s.submitted_at) filter (where s.status = 'accepted') as accepted_at,
    max(r.model_version) as model_version
from pulso.forecast_cycles c
left join pulso.forecast_runs r on r.cycle_id = c.cycle_id
left join pulso.submissions s on s.client_run_id = r.client_run_id
group by c.cycle_id, c.data_cutoff, c.closes_at;

alter table pulso.model_artifacts       enable row level security;
alter table pulso.performance_snapshots enable row level security;
alter table pulso.pipeline_runs         enable row level security;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant all on pulso.model_artifacts, pulso.performance_snapshots, pulso.pipeline_runs to service_role;
    grant all on all sequences in schema pulso to service_role;
  end if;
end
$$;
