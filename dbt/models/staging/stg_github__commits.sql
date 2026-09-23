-- One row per commit url. API rows (my repos, every branch, + mine elsewhere) win over
-- the export's git log, which fills history before the nightly runs started.
with unioned as (
    select
        html_url as url,
        split_part(html_url, '/', 4) || '/' || split_part(html_url, '/', 5) as repository,
        sha,
        commit__author__name as author_name,
        commit__author__email as author_email,
        author__login as author_login,
        author__type as author_type,
        commit__author__date as authored_at,
        commit__committer__date as committed_at,
        commit__message as message,
        stats__additions as additions,
        stats__deletions as deletions,
        1 as priority
    from {{ source('github', 'commits') }}
    union all
    select
        html_url, split_part(html_url, '/', 4) || '/' || split_part(html_url, '/', 5), sha,
        commit__author__name, commit__author__email, author__login, author__type,
        commit__author__date, commit__committer__date, commit__message,
        stats__additions, stats__deletions,
        1
    from {{ source('github', 'external_commits') }}
    union all
    select
        'https://github.com/' || repository || '/commit/' || sha, repository, sha,
        author_name, author_email, null, null,
        authored_at::timestamptz, committed_at::timestamptz, message,
        additions, deletions,
        2
    from {{ source('github', 'export_commits') }}
)
select distinct on (url) *
from unioned
order by url, priority
