-- One row per PR url: my repos from the pulls endpoint, mine elsewhere from search,
-- history from the export. State comes from the freshest source; the export fills
-- fields the others lack (e.g. bodies and branch names of old PRs).
with unioned as (
    select
        html_url as url, base__repo__full_name as repository, number,
        user__login as author_login, user__type as author_type,
        title, body, state, draft, base__ref as base_ref, head__ref as head_ref,
        created_at, updated_at, closed_at, merged_at, merge_commit_sha,
        1 as priority
    from {{ source('github', 'pull_requests') }}
    union all
    select
        html_url, replace(repository_url, 'https://api.github.com/repos/', ''), number,
        user__login, user__type,
        title, body, state, draft, null, null,
        created_at, updated_at, closed_at, pull_request__merged_at, null,
        1
    from {{ source('github', 'external_issues') }}
    where pull_request__html_url is not null
    union all
    select
        url, replace(repository, 'https://github.com/', ''), split_part(url, '/', 7)::int,
        split_part("user", '/', 4), null,
        title, body, case when closed_at is null then 'open' else 'closed' end, work_in_progress,
        base__ref, head__ref,
        created_at::timestamptz, null, closed_at::timestamptz, merged_at::timestamptz, merge_commit_sha,
        2
    from {{ source('github', 'export_pull_requests') }}
),
freshest as (
    select distinct on (url) url, repository, number, title, state, created_at, updated_at, closed_at, merged_at
    from unioned
    order by url, priority
),
filled as (
    select
        url,
        {{ first_non_null('author_login') }} as author_login,
        {{ first_non_null('author_type') }} as author_type,
        {{ first_non_null('body') }} as body,
        {{ first_non_null('draft') }} as draft,
        {{ first_non_null('base_ref') }} as base_ref,
        {{ first_non_null('head_ref') }} as head_ref,
        {{ first_non_null('merge_commit_sha') }} as merge_commit_sha
    from unioned
    group by url
)
select f.*, x.author_login, x.author_type, x.body, x.draft, x.base_ref, x.head_ref, x.merge_commit_sha
from freshest f
join filled x using (url)
