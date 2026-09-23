select
    session_id,
    min(started_at) as started_at,
    max(ended_at) as ended_at,
    min(day) as day,
    count(*) as tracks,
    extract(epoch from max(ended_at) - min(started_at)) / 60 as minutes,
    count(distinct artist) as artists,
    mode() within group (order by artist) as top_artist
from {{ ref('music_listens') }}
group by session_id
