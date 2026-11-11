{{ config(alias='commits') }}
-- Every commit in my repos (all branches, any author) plus mine elsewhere.
-- is_mine: one of my emails (git emails are often not linked to the account), my login,
-- or my name on commits made without an email. Forks carry upstream history: filter
-- on is_fork_repo and not is_mine to drop it.
with repos as (
    -- dlt merges repos on `id`, so a repo deleted and recreated under the same
    -- full_name leaves a stale row behind; joining both would duplicate every
    -- commit of that repo. Keep one row per full_name, preferring the newest id.
    select distinct on (full_name) full_name, fork
    from {{ optional_input('github', 'repos', {'id': 'bigint', 'full_name': 'text', 'fork': 'boolean'}, ['full_name']) }}
    order by full_name, id desc nulls last
)
select
    c.*,
    split_part(c.message, E'\n', 1) as subject,
    -- Matching none of my identities means not mine (unlinked emails have no login).
    coalesce(lower(c.author_email) in {{ github_my_emails() }}
        or c.author_login = '{{ var("github_login") }}'
        or (coalesce(c.author_email, '') = '' and c.author_name = '{{ var("github_login") }}'), false) as is_mine,
    {{ github_is_bot('c.author_login', 'c.author_type') }} or c.author_email like '%[bot]%' as is_bot,
    coalesce(r.fork, false) as is_fork_repo,
    split_part(c.url, '/', 4) <> '{{ var("github_login") }}' as is_external_repo,
    (c.authored_at at time zone '{{ var("timezone") }}')::date as day,
    extract(hour from c.authored_at at time zone '{{ var("timezone") }}')::int as hour
from {{ ref('stg_github__commits') }} c
left join repos r on r.full_name = c.repository
