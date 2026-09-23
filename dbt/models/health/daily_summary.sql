-- One row per day: activity, heart, body and sleep
with daily as (
    select day, metric_name, sum(value) as total, avg(value) as mean, min(min_value) as lo, max(max_value) as hi
    from {{ ref('stg_health__metrics') }}
    group by 1, 2
),
metrics as (
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
)
select *, extract(isodow from day)::int as weekday
from metrics
full join {{ ref('stg_health__sleep') }} using (day)
