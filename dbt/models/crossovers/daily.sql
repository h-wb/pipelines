-- One row per day across every source
select
    d.day,
    d.steps,
    d.active_kcal,
    d.exercise_min,
    d.daylight_min,
    d.resting_hr,
    d.hrv_ms,
    d.sleep_h,
    d.bedtime_h,
    coalesce(l.listens, 0) as listens,
    coalesce(l.late_listens, 0) as late_listens,
    coalesce(r.rides, 0) as rides,
    coalesce(r.ride_min, 0) as ride_min,
    coalesce(w.workout_min, 0) as workout_min
from {{ ref('daily_summary') }} d
left join (
    select day, count(*) as listens, count(*) filter (where hour < 5) as late_listens
    from {{ ref('listens') }} group by 1
) l using (day)
left join (select day, count(*) as rides, sum(minutes) as ride_min from {{ ref('rides') }} group by 1) r using (day)
left join (select day, sum(duration_min) as workout_min from {{ ref('workout_summary') }} group by 1) w using (day)
-- Health data starts in 2020
where d.day >= '2020-01-01'
