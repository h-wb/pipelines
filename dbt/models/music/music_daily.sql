{{ config(alias='daily') }}
-- One row per calendar day since the first listen (including silent days), with streaks
with days as (
    select generate_series(min(day), max(day), interval '1 day')::date as day from {{ ref('music_listens') }}
),
counts as (
    select day, count(*) as listens, count(distinct artist) as artists,
           count(*) filter (where is_new_artist) as new_artists,
           sum(extract(epoch from ended_at - started_at)) / 60 as minutes
    from {{ ref('music_listens') }} group by 1
),
d as (
    select days.day, coalesce(listens, 0) as listens, coalesce(artists, 0) as artists,
           coalesce(new_artists, 0) as new_artists, coalesce(minutes, 0) as minutes
    from days left join counts using (day)
)
select
    *,
    -- consecutive days with music share a streak id
    case when listens > 0 then day - (row_number() over (partition by listens > 0 order by day))::int end as streak_id
from d
