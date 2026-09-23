{{ config(indexes=[{'columns': ['measured_at']}]) }}
-- Minute-level heart rate, indexed for time-window joins
select measured_at, value as bpm
from {{ ref('stg_health__metrics') }}
where metric_name = 'heart_rate'
