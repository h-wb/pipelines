-- Bike Share only estimates distance (duration x a fixed average speed), so the
-- straight-line distance between the two stations is the only real distance we have.
with trips as (
    select
        t.rental_id as ride_id,
        to_timestamp(t.sd / 1000.0) as started_at,
        to_timestamp(t.ed / 1000.0) as ended_at,
        (t.ed - t.sd) / 60000.0 as minutes,
        t.ss as start_station,
        t.es as end_station,
        t.distance_in_meters / 1000.0 as estimated_km,
        t.bike__displayed_number as bike,
        -- locations are [lon, lat] arrays loaded as child tables
        max(s.value) filter (where s._dlt_list_idx = 0) as start_lon,
        max(s.value) filter (where s._dlt_list_idx = 1) as start_lat,
        max(e.value) filter (where e._dlt_list_idx = 0) as end_lon,
        max(e.value) filter (where e._dlt_list_idx = 1) as end_lat
    from {{ source('bikeshare', 'trips') }} t
    left join {{ source('bikeshare', 'trips__start_location') }} s on s._dlt_parent_id = t._dlt_id
    left join {{ source('bikeshare', 'trips__end_location') }} e on e._dlt_parent_id = t._dlt_id
    group by 1, 2, 3, 4, 5, 6, 7, 8
)
select
    *,
    (started_at at time zone '{{ var("timezone") }}')::date as day,
    -- haversine, km
    2 * 6371 * asin(sqrt(
        power(sin(radians(end_lat - start_lat) / 2), 2)
        + cos(radians(start_lat)) * cos(radians(end_lat)) * power(sin(radians(end_lon - start_lon) / 2), 2)
    )) as direct_km
from trips
