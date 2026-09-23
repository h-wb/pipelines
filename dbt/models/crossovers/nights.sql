-- Listening after midnight vs the sleep that ends that morning
select
    d.day,
    d.late_listens,
    case
        when d.late_listens = 0 then '0 · none'
        when d.late_listens <= 5 then '1 · 1-5 tracks'
        when d.late_listens <= 15 then '2 · 6-15 tracks'
        else '3 · 16+ tracks'
    end as late_listening,
    d.sleep_h,
    s.deep_h,
    s.rem_h,
    d.bedtime_h,
    d.hrv_ms,
    d.resting_hr
from {{ ref('daily') }} d
join {{ ref('daily_summary') }} s using (day)
where d.sleep_h is not null
