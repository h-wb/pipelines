{{ config(alias='contributions') }}
-- GitHub's contribution calendar (what the profile heatmap shows), one row per day
select
    date::date as day,
    contribution_count
from {{ source('github', 'contributions_daily') }}
