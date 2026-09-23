-- One row per night, keyed by the day the sleep ends.
-- Before 2025 only time in bed was recorded; stages start in 2025.
with nights as (
    select
        left(date, 10)::date as day,
        max(case when total_sleep > 0 then total_sleep else coalesce(in_bed__v_double, in_bed) end) as sleep_h,
        max(coalesce(deep__v_double, deep)) filter (where total_sleep > 0) as deep_h,
        max(coalesce(rem__v_double, rem)) filter (where total_sleep > 0) as rem_h,
        max(coalesce(core__v_double, core)) filter (where total_sleep > 0) as core_h,
        max(coalesce(awake__v_double, awake)) filter (where total_sleep > 0) as awake_h,
        max(coalesce(nullif(sleep_start, ''), in_bed_start)) as sleep_start,
        max(coalesce(nullif(sleep_end, ''), in_bed_end)) as sleep_end
    from {{ source('apple_health', 'metrics') }}
    where metric_name = 'sleep_analysis'
    group by 1
)
select
    *,
    -- Hours relative to midnight (23:30 -> -0.5, 01:15 -> 1.25)
    substr(sleep_start, 12, 2)::int + substr(sleep_start, 15, 2)::int / 60.0
        - case when substr(sleep_start, 12, 2)::int >= 12 then 24 else 0 end as bedtime_h,
    substr(sleep_end, 12, 2)::int + substr(sleep_end, 15, 2)::int / 60.0 as wake_h
from nights
