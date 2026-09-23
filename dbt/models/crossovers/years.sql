-- Year in review
with y as (select extract(year from day)::int as year, * from {{ ref('daily') }}),
listens as (select extract(year from day)::int as year, * from {{ ref('listens') }}),
workouts as (select extract(year from day)::int as year, * from {{ ref('workout_summary') }}),
rides as (select extract(year from day)::int as year, * from {{ ref('rides') }}),
best_sleep as (
    select distinct on (year) year, to_char(month, 'FMMonth') as best_sleep_month
    from (select year, date_trunc('month', day) as month, avg(sleep_h) as sleep_h, count(sleep_h) as n from y group by 1, 2) m
    where n >= 10
    order by year, sleep_h desc
),
biggest_day as (
    select distinct on (year) year, to_char(day, 'FMMonth FMDD') as biggest_step_day, steps as biggest_step_count
    from y where steps is not null order by year, steps desc
)
select
    y.year,
    sum(y.steps) as steps,
    avg(y.steps) as avg_steps,
    avg(y.sleep_h) as avg_sleep_h,
    avg(y.resting_hr) as avg_resting_hr,
    sum(y.listens) as listens,
    (select count(distinct artist) from listens l where l.year = y.year) as artists,
    (select mode() within group (order by artist) from listens l where l.year = y.year) as top_artist,
    (select mode() within group (order by track || ' — ' || artist) from listens l where l.year = y.year) as top_track,
    (select mode() within group (order by artist) from listens l where l.year = y.year and workout_type is not null) as top_workout_artist,
    (select count(*) from workouts w where w.year = y.year) as workouts,
    (select sum(duration_min) / 60 from workouts w where w.year = y.year) as workout_hours,
    (select mode() within group (order by workout_type) from workouts w where w.year = y.year) as top_workout_type,
    (select count(*) from rides r where r.year = y.year) as rides,
    (select sum(km) from rides r where r.year = y.year) as ride_km,
    max(b.best_sleep_month) as best_sleep_month,
    max(d.biggest_step_day) as biggest_step_day,
    max(d.biggest_step_count) as biggest_step_count
from y
left join best_sleep b using (year)
left join biggest_day d using (year)
group by y.year
