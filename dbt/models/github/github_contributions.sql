{{ config(alias='contributions') }}
-- GitHub's contribution calendar (what the profile heatmap shows), one row per day
with contributions as (
    {{ optional_input('github', 'contributions_daily', {'date': 'text', 'contribution_count': 'bigint'}, ['date']) }}
)
select
    date::date as day,
    contribution_count
from contributions
