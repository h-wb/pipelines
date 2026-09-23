select min(day) as started_on, max(day) as ended_on, count(*) as days, sum(listens) as listens
from {{ ref('music_daily') }}
where streak_id is not null
group by streak_id
