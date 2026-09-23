-- Bike rides with the heart rate and energy measured during them
select
    t.*,
    t.km / nullif(t.minutes / 60, 0) as kmh,
    hr.avg_bpm,
    hr.max_bpm,
    hr.samples as hr_samples,
    ae.kcal
from {{ ref('stg_bikeshare__trips') }} t
left join lateral (
    select avg(bpm) as avg_bpm, max(bpm) as max_bpm, count(*) as samples
    from {{ ref('int_heart_rate') }} h
    where h.measured_at between t.started_at and t.ended_at
) hr on true
left join lateral (
    select sum(kcal) as kcal
    from {{ ref('int_active_energy') }} e
    where e.measured_at between t.started_at and t.ended_at
) ae on true
