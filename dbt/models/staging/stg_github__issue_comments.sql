-- One row per comment url on issues and PRs in my repos
with issue_comments as (
    {{ optional_input('github', 'issue_comments', {
        'html_url': 'text', 'user__login': 'text', 'user__type': 'text', 'body': 'text',
        'created_at': 'timestamptz', 'updated_at': 'timestamptz'
    }, ['html_url']) }}
),
export_issue_comments as (
    {{ optional_input('github', 'export_issue_comments', {
        'url': 'text', 'user': 'text', 'body': 'text', 'created_at': 'timestamptz'
    }, ['url']) }}
),
unioned as (
    select
        html_url as url,
        split_part(html_url, '/', 4) || '/' || split_part(html_url, '/', 5) as repository,
        split_part(html_url, '#', 1) as parent_url,
        user__login as author_login, user__type as author_type,
        body, created_at, updated_at,
        1 as priority
    from issue_comments
    union all
    select
        url, split_part(url, '/', 4) || '/' || split_part(url, '/', 5), split_part(url, '#', 1),
        split_part("user", '/', 4), null,
        body, created_at::timestamptz, null,
        2
    from export_issue_comments
)
select distinct on (url) *
from unioned
order by url, priority
