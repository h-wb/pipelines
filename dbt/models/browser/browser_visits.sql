{{ config(alias='visits') }}
-- Every visit, with local day/hour. Redirect hops and reloads are flagged rather
-- than dropped; filter on is_navigation for "pages I actually went to".
select
    *,
    visit_type not in ('redirect_permanent', 'redirect_temporary', 'reload', 'embed', 'framed_link') as is_navigation,
    (visited_at at time zone '{{ var("timezone") }}')::date as day,
    extract(hour from visited_at at time zone '{{ var("timezone") }}')::int as hour
from {{ ref('stg_browser__visits') }}
