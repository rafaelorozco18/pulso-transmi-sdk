-- Índices para llaves foráneas y PK en observation_revisions
-- (avisos unindexed_foreign_keys y no_primary_key del Performance Advisor de Supabase).

alter table pulso.observation_revisions
    add column revision_id bigint generated always as identity primary key;

create index context_ingestion_run_idx               on pulso.context (ingestion_run_id);
create index drift_signals_model_version_idx         on pulso.drift_signals (model_version);
create index drift_signals_profile_version_idx       on pulso.drift_signals (profile_version_id);
create index drift_signals_station_idx               on pulso.drift_signals (station_id);
create index forecast_runs_model_version_idx         on pulso.forecast_runs (model_version);
create index ingestion_cursors_last_run_idx          on pulso.ingestion_cursors (last_run_id);
create index ingestion_runs_snapshot_idx             on pulso.ingestion_runs (snapshot_id);
create index model_versions_profile_version_idx      on pulso.model_versions (profile_version_id);
create index observation_revisions_run_idx           on pulso.observation_revisions (ingestion_run_id);
create index observation_revisions_observation_idx   on pulso.observation_revisions (station_id, observed_at);
create index observations_ingestion_run_idx          on pulso.observations (ingestion_run_id);
create index retraining_decisions_active_model_idx   on pulso.retraining_decisions (active_model_version);
create index retraining_decisions_candidate_model_idx on pulso.retraining_decisions (candidate_model_version);
create index station_profiles_station_idx            on pulso.station_profiles (station_id);
