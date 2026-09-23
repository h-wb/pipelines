select
    rental_id as ride_id,
    to_timestamp(sd / 1000.0) as started_at,
    to_timestamp(ed / 1000.0) as ended_at,
    (to_timestamp(sd / 1000.0) at time zone '{{ var("timezone") }}')::date as day,
    (ed - sd) / 60000.0 as minutes,
    ss as start_station,
    es as end_station,
    distance_in_meters / 1000.0 as km
from {{ source('bikeshare', 'trips') }}
