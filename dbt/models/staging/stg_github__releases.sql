-- One row per release url in my repos
with releases as (
    {{ optional_input('github', 'releases', {
        'html_url': 'text', 'author__login': 'text', 'author__type': 'text',
        'name': 'text', 'tag_name': 'text', 'body': 'text', 'prerelease': 'boolean',
        'created_at': 'timestamptz', 'published_at': 'timestamptz'
    }, ['html_url']) }}
),
export_releases as (
    {{ optional_input('github', 'export_releases', {
        'url': 'text', 'repository': 'text', 'user': 'text',
        'name': 'text', 'tag_name': 'text', 'body': 'text', 'prerelease': 'boolean',
        'created_at': 'timestamptz', 'published_at': 'timestamptz'
    }, ['url']) }}
),
unioned as (
    select
        html_url as url,
        split_part(html_url, '/', 4) || '/' || split_part(html_url, '/', 5) as repository,
        author__login as author_login, author__type as author_type,
        name, tag_name, body, prerelease, created_at, published_at,
        1 as priority
    from releases
    union all
    select
        url, replace(repository, 'https://github.com/', ''),
        split_part("user", '/', 4), null,
        name, tag_name, body, prerelease, created_at::timestamptz, published_at::timestamptz,
        2
    from export_releases
)
select distinct on (url) *
from unioned
order by url, priority
