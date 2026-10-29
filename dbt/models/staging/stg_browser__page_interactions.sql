-- Firefox's per-page interaction records (time in view, typing, scrolling).
-- Unlike visits, these timestamps and durations are in milliseconds.
-- Optional: older profiles and some Firefox versions have no interaction table.
with interactions as (
    {{ optional_input('browser_history', 'zen_places_metadata', {
        'id': 'bigint', 'place_id': 'bigint', 'created_at': 'bigint', 'updated_at': 'bigint',
        'total_view_time': 'bigint', 'typing_time': 'bigint', 'key_presses': 'bigint',
        'scrolling_time': 'bigint', 'scrolling_distance': 'bigint'
    }, ['id', 'place_id']) }}
),
places as (
    {{ optional_input('browser_history', 'zen_places', {
        'id': 'bigint', 'url': 'text', 'title': 'text', 'origin_id': 'bigint'
    }, ['id', 'url']) }}
),
origins as (
    {{ optional_input('browser_history', 'zen_origins', {'id': 'bigint', 'host': 'text'}, ['id']) }}
)
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
from interactions m
join places p on p.id = m.place_id
left join origins o on o.id = p.origin_id
