{{ config(alias='pull_requests') }}
-- Pull requests in my repos (any author) and mine elsewhere. status = open / merged / closed
select
    *,
    case when merged_at is not null then 'merged' else state end as status,
    author_login = '{{ var("github_login") }}' as is_mine,
    {{ github_is_bot('author_login', 'author_type') }} as is_bot,
    split_part(url, '/', 4) <> '{{ var("github_login") }}' as is_external_repo,
    round(extract(epoch from merged_at - created_at) / 3600.0, 1) as hours_to_merge,
    (created_at at time zone '{{ var("timezone") }}')::date as day
from {{ ref('stg_github__pull_requests') }}
