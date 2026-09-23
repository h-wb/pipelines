{{ config(alias='issues') }}
-- Issues (not PRs) in my repos (any author) and mine elsewhere
select
    *,
    author_login = '{{ var("github_login") }}' as is_mine,
    {{ github_is_bot('author_login', 'author_type') }} as is_bot,
    split_part(url, '/', 4) <> '{{ var("github_login") }}' as is_external_repo,
    (created_at at time zone '{{ var("timezone") }}')::date as day
from {{ ref('stg_github__issues') }}
