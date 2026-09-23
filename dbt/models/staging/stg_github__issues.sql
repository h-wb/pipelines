-- One row per issue url (the API endpoints also return PRs; those are dropped here)
with unioned as (
    select
        html_url as url, replace(repository_url, 'https://api.github.com/repos/', '') as repository, number,
        user__login as author_login, user__type as author_type,
        title, body, state, created_at, updated_at, closed_at,
        1 as priority
    from {{ source('github', 'issues') }}
    where pull_request__url is null
    union all
    select
        html_url, replace(repository_url, 'https://api.github.com/repos/', ''), number,
        user__login, user__type,
        title, body, state, created_at, updated_at, closed_at,
        1
    from {{ source('github', 'external_issues') }}
    where pull_request__html_url is null
    union all
    select
        url, replace(repository, 'https://github.com/', ''), split_part(url, '/', 7)::int,
        split_part("user", '/', 4), null,
        title, body, case when closed_at is null then 'open' else 'closed' end,
        created_at::timestamptz, updated_at::timestamptz, closed_at::timestamptz,
        2
    from {{ source('github', 'export_issues') }}
)
select distinct on (url) *
from unioned
order by url, priority
