-- Corrige 20260927150000: el pooler de Supabase ignora `options=-csearch_path`, así
-- que MLflow creó sus tablas en `public`, expuesto por la API REST (anon/authenticated
-- con todos los privilegios y sin RLS). Se mueven a `mlflow` y MLflow pasa a usar un
-- rol propio cuyo search_path es `mlflow` (configuración de rol, que sí aplica a
-- través del pooler). La contraseña del rol NO se versiona:
--   alter role mlflow_writer with password '<secreto>';

do $$
declare
    tbl text;
begin
    foreach tbl in array array[
        'alembic_version', 'assessments', 'budget_policies', 'datasets', 'endpoint_bindings', 'endpoint_model_mappings',
        'endpoint_tags', 'endpoints', 'entity_associations', 'evaluation_dataset_records', 'evaluation_dataset_tags', 'evaluation_datasets',
        'experiment_tags', 'experiments', 'guardrail_configs', 'guardrails', 'input_tags', 'inputs',
        'issues', 'jobs', 'label_schemas', 'latest_metrics', 'logged_model_metrics', 'logged_model_params',
        'logged_model_tags', 'logged_models', 'mcp_access_endpoints', 'mcp_server_aliases', 'mcp_server_tags', 'mcp_server_version_tags',
        'mcp_server_versions', 'mcp_servers', 'metrics', 'model_definitions', 'model_version_tags', 'model_versions',
        'online_scoring_configs', 'params', 'registered_model_aliases', 'registered_model_tags', 'registered_models', 'review_queue_items',
        'review_queue_label_schemas', 'review_queue_users', 'review_queues', 'runs', 'scorer_versions', 'scorers',
        'secrets', 'span_metrics', 'spans', 'tags', 'trace_info', 'trace_metrics',
        'trace_request_metadata', 'trace_tags', 'webhook_events', 'webhooks', 'workspaces'
    ] loop
        if to_regclass(format('public.%I', tbl)) is not null then
            execute format('revoke all on public.%I from anon, authenticated', tbl);
            execute format('alter table public.%I set schema mlflow', tbl);
        end if;
    end loop;
    if to_regprocedure('public.prevent_secrets_aad_mutation()') is not null then
        alter function public.prevent_secrets_aad_mutation() set schema mlflow;
    end if;
end
$$;

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'mlflow_writer') then
        create role mlflow_writer with login nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
    end if;
end
$$;

alter role mlflow_writer set search_path = mlflow;
alter role mlflow_writer set statement_timeout = '60s';
grant usage, create on schema mlflow to mlflow_writer;
-- postgres administra el rol y puede leer sus tablas (vistas del dashboard).
grant mlflow_writer to postgres with inherit true, set true;

do $$
declare
    obj record;
begin
    for obj in select tablename from pg_tables where schemaname = 'mlflow' loop
        execute format('alter table mlflow.%I owner to mlflow_writer', obj.tablename);
    end loop;
    if to_regprocedure('mlflow.prevent_secrets_aad_mutation()') is not null then
        alter function mlflow.prevent_secrets_aad_mutation() owner to mlflow_writer;
    end if;
end
$$;

revoke all on all tables in schema mlflow from anon, authenticated;
