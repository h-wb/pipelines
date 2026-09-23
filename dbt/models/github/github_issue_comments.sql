{{ config(alias='issue_comments') }}
-- Comments on issues and pull requests in my repos
select
    *,
    case when parent_url like '%/pull/%' then 'pull_request' else 'issue' end as parent_type,
    author_login = '{{ var("github_login") }}' as is_mine,
    {{ github_is_bot('author_login', 'author_type') }} as is_bot,
    (created_at at time zone '{{ var("timezone") }}')::date as day
from {{ ref('stg_github__issue_comments') }}
