-- The artist and track that defined each month
with plays as (
    select date_trunc('month', day)::date as month, artist, track, count(*) as plays
    from {{ ref('music_listens') }}
    group by 1, 2, 3
),
artists as (
    select month, artist, sum(plays) as plays, sum(sum(plays)) over (partition by month) as month_plays,
           row_number() over (partition by month order by sum(plays) desc, artist) as rn
    from plays
    group by 1, 2
),
tracks as (
    select distinct on (month) month, track || ' — ' || artist as top_track, plays as top_track_plays
    from plays
    order by month, plays desc, track
)
select
    a.month,
    a.artist as top_artist,
    a.plays as top_artist_plays,
    a.plays::float / a.month_plays as top_artist_share,
    a.month_plays as listens,
    t.top_track,
    t.top_track_plays,
    ar.cover_url
from artists a
join tracks t using (month)
left join {{ ref('artists') }} ar on ar.artist = a.artist
where a.rn = 1
