-- One row per station with how often it was used, for the map
with uses as (
    select start_station as station, start_lat as lat, start_lon as lon, day, 1 as starts, 0 as ends from {{ ref('stg_bikeshare__trips') }}
    union all
    select end_station, end_lat, end_lon, day, 0, 1 from {{ ref('stg_bikeshare__trips') }}
)
select
    station,
    avg(lat) as latitude,
    avg(lon) as longitude,
    sum(starts) as starts,
    sum(ends) as ends,
    count(*) as visits,
    min(day) as first_used,
    max(day) as last_used
from uses
group by station
