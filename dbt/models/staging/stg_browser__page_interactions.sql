-- Firefox's per-page interaction records (time in view, typing, scrolling).
-- Unlike visits, these timestamps and durations are in milliseconds.
select
    m.id as interaction_id,
    to_timestamp(m.created_at / 1000.0) as started_at,
    to_timestamp(m.updated_at / 1000.0) as updated_at,
    p.url,
    p.title,
    regexp_replace(o.host, '^www\.', '') as domain,
    m.total_view_time / 1000.0 as view_seconds,
    m.typing_time / 1000.0 as typing_seconds,
    m.key_presses,
    m.scrolling_time / 1000.0 as scrolling_seconds,
    m.scrolling_distance
from {{ source('browser_history', 'zen_places_metadata') }} m
join {{ source('browser_history', 'zen_places') }} p on p.id = m.place_id
left join {{ source('browser_history', 'zen_origins') }} o on o.id = p.origin_id
