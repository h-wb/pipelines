{{ config(indexes=[{'columns': ['measured_at']}]) }}
select measured_at, value as kcal
from {{ ref('stg_health__metrics') }}
where metric_name = 'active_energy'
