-- One value per metric and timestamp. Dates are local-time strings
-- ("YYYY-MM-DD HH:MM:SS -ZZZZ"); a few minutes appear under two source strings.
select
    metric_name,
    date::timestamptz as measured_at,
    left(date, 10)::date as day,
    max(units) as units,
    -- dlt splits values into an int column and a __v_double column
    max(coalesce(qty, avg__v_double, avg)) as value,
    min(coalesce(min__v_double, min, qty)) as min_value,
    max(coalesce(max__v_double, max, qty)) as max_value
from {{ source('apple_health', 'metrics') }}
where metric_name <> 'sleep_analysis'
group by metric_name, date
