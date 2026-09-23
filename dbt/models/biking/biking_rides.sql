{{ config(alias='rides') }}
-- Every ride with local time context. Commutes = weekday rides in the morning or evening rush.
select
    ride_id,
    started_at,
    ended_at,
    day,
    extract(isodow from day)::int as weekday,
    extract(hour from started_at at time zone '{{ var("timezone") }}')::int as hour,
    minutes,
    start_station,
    end_station,
    start_station || ' → ' || end_station as route,
    start_station = end_station as is_round_trip,
    estimated_km,
    direct_km,
    -- straight-line, so a lower bound on the real speed
    direct_km / nullif(minutes / 60, 0) as direct_kmh,
    bike,
    case
        when extract(isodow from day) <= 5 and extract(hour from started_at at time zone '{{ var("timezone") }}') between 7 and 9 then 'Morning commute'
        when extract(isodow from day) <= 5 and extract(hour from started_at at time zone '{{ var("timezone") }}') between 16 and 18 then 'Evening commute'
        when extract(isodow from day) >= 6 then 'Weekend'
        else 'Weekday, off-peak'
    end as ride_type,
    start_lat, start_lon, end_lat, end_lon
from {{ ref('stg_bikeshare__trips') }}
