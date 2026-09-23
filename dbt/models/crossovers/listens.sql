-- Every listen, tagged with what was going on at the time
select
    l.*,
    w.workout_type,
    r.ride_id,
    hr.bpm,
    -- Heart rate relative to that day's average, so busy days don't dominate
    hr.bpm - d.avg_hr as bpm_vs_day
from {{ ref('stg_listenbrainz__listens') }} l
left join lateral (
    select workout_type from {{ ref('workout_summary') }} w
    where l.started_at between w.started_at and w.ended_at
    limit 1
) w on true
left join lateral (
    select ride_id from {{ ref('int_rides') }} r
    where l.started_at between r.started_at and r.ended_at
    limit 1
) r on true
left join lateral (
    -- Resting heart rate is only sampled every few minutes, so widen the window a little
    select avg(bpm) as bpm from {{ ref('int_heart_rate') }} h
    where h.measured_at between l.started_at - interval '2 minutes' and l.ended_at + interval '2 minutes'
) hr on true
left join {{ ref('daily_summary') }} d on d.day = l.day
