select
    start_station,
    end_station,
    route,
    count(*) as rides,
    avg(minutes) as avg_minutes,
    min(minutes) as fastest_minutes,
    max(direct_km) as direct_km,
    max(direct_km) / nullif(min(minutes) / 60, 0) as best_direct_kmh,
    min(day) as first_ridden,
    max(day) as last_ridden
from {{ ref('biking_rides') }}
group by 1, 2, 3
