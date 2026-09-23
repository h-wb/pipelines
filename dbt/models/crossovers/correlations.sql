-- Pairwise correlations between daily metrics: same day and next day, all time and per year.
-- Bedtime is left out: Auto Export's sleep start includes naps, so it isn't a reliable bedtime.
with long as (
    select day, metric, value
    from {{ ref('daily') }},
    lateral (values
        ('Steps', steps::float), ('Active energy', active_kcal), ('Exercise min', exercise_min),
        ('Daylight min', daylight_min), ('Resting HR', resting_hr), ('HRV', hrv_ms), ('Sleep h', sleep_h),
        ('Listens', listens::float), ('Late listens', late_listens::float),
        ('Ride min', ride_min), ('Workout min', workout_min)
    ) v (metric, value)
    where value is not null
),
pairs as (
    select a.metric as metric_a, b.metric as metric_b, o.offset_days, a.day, a.value as va, b.value as vb
    from long a
    cross join (values (0), (1)) o (offset_days)
    join long b on b.day = a.day + o.offset_days and (o.offset_days = 1 or a.metric <> b.metric)
)
select
    metric_a,
    metric_b,
    case offset_days when 0 then 'Same day' else 'Next day' end as timing,
    period,
    corr(va, vb) as r,
    count(*) as days,
    -- Same-day pairs are symmetric; flag one of each A/B, B/A pair so lists can skip it
    offset_days = 0 and metric_a > metric_b as is_mirror
from pairs,
lateral (values ('All time'), (extract(year from day)::text)) p (period)
group by metric_a, metric_b, offset_days, timing, period
-- corr() is null when one side never varies (e.g. no rides before 2022)
having count(*) >= 30 and corr(va, vb) is not null
