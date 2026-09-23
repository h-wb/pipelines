-- One row per artist: how much, since when, and when they peaked
with monthly as (
    select artist, date_trunc('month', day)::date as month, count(*) as plays
    from {{ ref('music_listens') }}
    group by 1, 2
),
peak as (
    select distinct on (artist) artist, month as peak_month, plays as peak_month_plays
    from monthly
    order by artist, plays desc, month
),
covers as (
    select distinct on (artist) artist, cover_url
    from (select artist, cover_url, count(*) as n from {{ ref('music_listens') }} where cover_url is not null group by 1, 2) c
    order by artist, n desc
)
select
    l.artist,
    count(*) as plays,
    count(distinct l.track) as tracks,
    count(distinct l.day) as days_listened,
    count(distinct date_trunc('month', l.day)) as months_listened,
    min(l.day) as first_listen,
    max(l.day) as last_listen,
    max(l.day) - min(l.day) as span_days,
    p.peak_month,
    p.peak_month_plays,
    c.cover_url
from {{ ref('music_listens') }} l
join peak p using (artist)
left join covers c using (artist)
group by l.artist, p.peak_month, p.peak_month_plays, c.cover_url
