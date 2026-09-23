select r.*, m.tracks, m.top_artist
from {{ ref('int_rides') }} r
left join (
    select ride_id, count(*) as tracks, mode() within group (order by artist) as top_artist
    from {{ ref('listens') }}
    where ride_id is not null
    group by 1
) m using (ride_id)
