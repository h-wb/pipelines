{{ config(alias='domains_daily') }}
-- Per site per local day: navigations and time spent (from page interactions).
with visits as (
    select day, domain, count(*) as visits
    from {{ ref('browser_visits') }}
    where is_navigation and domain is not null
    group by 1, 2
),
time_spent as (
    select
        (started_at at time zone '{{ var("timezone") }}')::date as day,
        domain,
        sum(view_seconds) / 60.0 as view_minutes,
        sum(typing_seconds) / 60.0 as typing_minutes,
        sum(key_presses) as key_presses
    from {{ ref('stg_browser__page_interactions') }}
    where domain is not null
    group by 1, 2
)
select
    coalesce(v.day, t.day) as day,
    coalesce(v.domain, t.domain) as domain,
    coalesce(v.visits, 0) as visits,
    coalesce(t.view_minutes, 0) as view_minutes,
    coalesce(t.typing_minutes, 0) as typing_minutes,
    coalesce(t.key_presses, 0) as key_presses
from visits v
full join time_spent t on t.day = v.day and t.domain = v.domain
