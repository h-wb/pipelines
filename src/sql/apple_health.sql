-- Reporting tables for Metabase, rebuilt after every apple_health load.
-- Dates in the raw tables are local-time strings ("YYYY-MM-DD HH:MM:SS -ZZZZ"),
-- so the first 10 characters are the local day.

drop table if exists apple_health_data.daily_summary;
create table apple_health_data.daily_summary as
with points as (
    -- one value per metric and timestamp: a few minutes appear under two source strings
    select
        metric_name,
        date,
        max(coalesce(qty, avg__v_double, avg)) as v,
        min(coalesce(min__v_double, min, qty)) as lo,
        max(coalesce(max__v_double, max, qty)) as hi
    from apple_health_data.metrics
    where metric_name <> 'sleep_analysis'
    group by 1, 2
),
daily as (
    select left(date, 10)::date as day, metric_name, sum(v) as total, avg(v) as mean, min(lo) as lo, max(hi) as hi
    from points
    group by 1, 2
),
activity as (
    select
        day,
        sum(total) filter (where metric_name = 'step_count') as steps,
        sum(total) filter (where metric_name = 'walking_running_distance') as distance_km,
        sum(total) filter (where metric_name = 'flights_climbed') as flights,
        sum(total) filter (where metric_name = 'active_energy') as active_kcal,
        sum(total) filter (where metric_name = 'basal_energy_burned') / 4.184 as basal_kcal,
        sum(total) filter (where metric_name = 'apple_exercise_time') as exercise_min,
        sum(total) filter (where metric_name = 'apple_stand_hour') as stand_hours,
        sum(total) filter (where metric_name = 'time_in_daylight') as daylight_min,
        sum(total) filter (where metric_name = 'mindful_minutes') as mindful_min,
        sum(total) filter (where metric_name = 'cycling_distance') as cycling_km,
        sum(total) filter (where metric_name = 'dietary_energy') / 4.184 as dietary_kcal,
        sum(total) filter (where metric_name = 'protein') as protein_g,
        sum(total) filter (where metric_name = 'carbohydrates') as carbs_g,
        sum(total) filter (where metric_name = 'total_fat') as fat_g,
        avg(mean) filter (where metric_name = 'heart_rate') as avg_hr,
        min(lo) filter (where metric_name = 'heart_rate') as min_hr,
        max(hi) filter (where metric_name = 'heart_rate') as max_hr,
        avg(mean) filter (where metric_name = 'resting_heart_rate') as resting_hr,
        avg(mean) filter (where metric_name = 'walking_heart_rate_average') as walking_hr,
        avg(mean) filter (where metric_name = 'heart_rate_variability') as hrv_ms,
        avg(mean) filter (where metric_name = 'blood_oxygen_saturation') as spo2_pct,
        avg(mean) filter (where metric_name = 'respiratory_rate') as respiratory_rate,
        avg(mean) filter (where metric_name = 'vo2_max') as vo2_max,
        avg(mean) filter (where metric_name = 'apple_sleeping_wrist_temperature') as wrist_temp_c,
        avg(mean) filter (where metric_name = 'weight_body_mass') as weight_kg,
        avg(mean) filter (where metric_name = 'body_fat_percentage') as body_fat_pct,
        avg(mean) filter (where metric_name = 'lean_body_mass') as lean_mass_kg,
        avg(mean) filter (where metric_name = 'body_mass_index') as bmi,
        avg(mean) filter (where metric_name = 'walking_speed') as walking_speed_kmh,
        avg(mean) filter (where metric_name = 'walking_step_length') as step_length_cm,
        avg(mean) filter (where metric_name = 'walking_asymmetry_percentage') as walking_asymmetry_pct,
        avg(mean) filter (where metric_name = 'environmental_audio_exposure') as env_audio_db,
        avg(mean) filter (where metric_name = 'headphone_audio_exposure') as headphone_audio_db
    from daily
    group by 1
),
sleep as (
    -- Before 2025 only time in bed was recorded; stages start in 2025
    select
        left(date, 10)::date as day,
        max(case when total_sleep > 0 then total_sleep else coalesce(in_bed__v_double, in_bed) end) as sleep_h,
        max(coalesce(deep__v_double, deep)) filter (where total_sleep > 0) as deep_h,
        max(coalesce(rem__v_double, rem)) filter (where total_sleep > 0) as rem_h,
        max(coalesce(core__v_double, core)) filter (where total_sleep > 0) as core_h,
        max(coalesce(awake__v_double, awake)) filter (where total_sleep > 0) as awake_h,
        max(coalesce(nullif(sleep_start, ''), in_bed_start)) as sleep_start,
        max(coalesce(nullif(sleep_end, ''), in_bed_end)) as sleep_end
    from apple_health_data.metrics
    where metric_name = 'sleep_analysis'
    group by 1
),
sleep_times as (
    select
        sleep.*,
        -- Hours relative to midnight (23:30 -> -0.5, 01:15 -> 1.25)
        substr(sleep_start, 12, 2)::int + substr(sleep_start, 15, 2)::int / 60.0
            - case when substr(sleep_start, 12, 2)::int >= 12 then 24 else 0 end as bedtime_h,
        substr(sleep_end, 12, 2)::int + substr(sleep_end, 15, 2)::int / 60.0 as wake_h
    from sleep
)
select *, extract(isodow from day)::int as weekday
from activity
full join sleep_times using (day);

drop table if exists apple_health_data.workout_summary;
create table apple_health_data.workout_summary as
select
    id,
    name as workout_type,
    left(start, 10)::date as day,
    start::timestamptz as started_at,
    duration / 60.0 as duration_min,
    active_energy_burned__qty as active_kcal,
    distance__qty as distance_km,
    coalesce(avg_heart_rate__qty, heart_rate__avg__qty) as avg_hr,
    coalesce(max_heart_rate__qty__v_double, max_heart_rate__qty, heart_rate__max__qty__v_double, heart_rate__max__qty) as max_hr,
    elevation_up__qty as elevation_up_m,
    is_indoor
from apple_health_data.workouts;
