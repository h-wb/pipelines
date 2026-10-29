with input as (
    {{ optional_input('apple_health', 'workouts', {
        'id': 'text', 'name': 'text', 'start': 'text', 'duration': 'double precision',
        'active_energy_burned__qty': 'double precision', 'distance__qty': 'double precision',
        'avg_heart_rate__qty': 'double precision', 'heart_rate__avg__qty': 'double precision',
        'max_heart_rate__qty__v_double': 'double precision', 'max_heart_rate__qty': 'double precision',
        'heart_rate__max__qty__v_double': 'double precision', 'heart_rate__max__qty': 'double precision',
        'elevation_up__qty': 'double precision', 'is_indoor': 'boolean'
    }, ['id', 'start']) }}
)
select
    id as workout_id,
    name as workout_type,
    left(start, 10)::date as day,
    start::timestamptz as started_at,
    start::timestamptz + make_interval(secs => duration) as ended_at,
    duration / 60.0 as duration_min,
    active_energy_burned__qty as active_kcal,
    distance__qty as distance_km,
    coalesce(avg_heart_rate__qty, heart_rate__avg__qty) as avg_hr,
    coalesce(max_heart_rate__qty__v_double, max_heart_rate__qty, heart_rate__max__qty__v_double, heart_rate__max__qty) as max_hr,
    elevation_up__qty as elevation_up_m,
    is_indoor
from input
