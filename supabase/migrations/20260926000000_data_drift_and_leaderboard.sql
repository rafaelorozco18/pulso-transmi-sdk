-- Data drift de la demanda (PSI) y leaderboard anonimizado para el dashboard.

-- Nueva señal: PSI de log(demanda) por estación frente a la referencia del
-- campeón (pipeline/drift.py). profile_shape ya estaba permitida.
alter table pulso.drift_signals drop constraint drift_signals_signal_check;
alter table pulso.drift_signals add constraint drift_signals_signal_check check (signal in (
    'level_vs_profile', 'wape_rolling', 'residual_bias', 'rain_sensitivity', 'event_sensitivity',
    'profile_shape', 'context_distribution', 'data_quality', 'demand_psi'));

create index if not exists leaderboard_snapshots_fetched_idx on pulso.leaderboard_snapshots (fetched_at desc);

-- Última tabla de cada ventana. El dashboard es público: solo se expone el
-- nombre del propio equipo (el de performance_snapshots); el resto queda anónimo.
create or replace view dashboard.leaderboard as
with me as (
    select leaderboard -> 'rolling_24h' ->> 'display_name' as display_name
    from pulso.performance_snapshots
    where leaderboard -> 'rolling_24h' ->> 'display_name' is not null
    order by computed_at desc
    limit 1
), latest as (
    select distinct on (window_name) window_name, fetched_at, payload
    from pulso.leaderboard_snapshots
    order by window_name, fetched_at desc
)
select
    l.window_name,
    l.fetched_at,
    (row ->> 'rank')::int                  as rank,
    row ->> 'kind'                         as kind,
    (row ->> 'accuracy')::double precision as accuracy,
    (row ->> 'coverage')::double precision as coverage,
    (row ->> 'eligible')::boolean          as eligible,
    (row ->> 'display_name') = me.display_name as is_me,
    case when (row ->> 'display_name') = me.display_name then row ->> 'display_name' end as display_name
from latest l
cross join lateral jsonb_array_elements(l.payload -> 'data') as row
left join me on true;

grant select on dashboard.leaderboard to dashboard_reader;
