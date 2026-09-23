{{ config(alias='releases') }}
-- Releases published in my repos
select
    *,
    author_login = '{{ var("github_login") }}' as is_mine,
    {{ github_is_bot('author_login', 'author_type') }} as is_bot,
    (coalesce(published_at, created_at) at time zone '{{ var("timezone") }}')::date as day
from {{ ref('stg_github__releases') }}
