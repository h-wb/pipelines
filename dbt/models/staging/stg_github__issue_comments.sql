-- One row per comment url on issues and PRs in my repos
with unioned as (
    select
        html_url as url,
        split_part(html_url, '/', 4) || '/' || split_part(html_url, '/', 5) as repository,
        split_part(html_url, '#', 1) as parent_url,
        user__login as author_login, user__type as author_type,
        body, created_at, updated_at,
        1 as priority
    from {{ source('github', 'issue_comments') }}
    union all
    select
        url, split_part(url, '/', 4) || '/' || split_part(url, '/', 5), split_part(url, '#', 1),
        split_part("user", '/', 4), null,
        body, created_at::timestamptz, null,
        2
    from {{ source('github', 'export_issue_comments') }}
)
select distinct on (url) *
from unioned
order by url, priority
