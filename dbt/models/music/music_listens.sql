{{ config(alias='listens') }}
-- Every listen with discovery flags and a listening-session id
with l as (
    select
        *,
        extract(isodow from day)::int as weekday,
        to_char(day, 'ID Dy') as weekday_label,
        case when cover_release_mbid is not null
            then 'https://coverartarchive.org/release/' || cover_release_mbid || '/front-250' end as cover_url,
        row_number() over (partition by artist order by started_at) = 1 as is_new_artist,
        row_number() over (partition by artist, track order by started_at) = 1 as is_new_track,
        -- a new session starts after 30 minutes without music
        coalesce(started_at - lag(ended_at) over (order by started_at) > interval '30 minutes', true) as starts_session
    from {{ ref('stg_listenbrainz__listens') }}
)
select *, sum(starts_session::int) over (order by started_at) as session_id
from l
