-- Tracks played the most times in a single day
select day, track, artist, count(*) as plays, max(cover_url) as cover_url
from {{ ref('music_listens') }}
group by 1, 2, 3
having count(*) >= 5
