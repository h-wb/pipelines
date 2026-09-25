-- One row per Zen visit with its page. Firefox stores visit times in microseconds.
-- visit_type codes: https://searchfox.org/mozilla-central/source/toolkit/components/places/nsINavHistoryService.idl
select
    v.id as visit_id,
    to_timestamp(v.visit_date / 1000000.0) as visited_at,
    v.from_visit as from_visit_id,
    p.id as place_id,
    p.url,
    p.title,
    regexp_replace(o.host, '^www\.', '') as domain,
    case v.visit_type
        when 1 then 'link' when 2 then 'typed' when 3 then 'bookmark' when 4 then 'embed'
        when 5 then 'redirect_permanent' when 6 then 'redirect_temporary' when 7 then 'download'
        when 8 then 'framed_link' when 9 then 'reload' else 'other'
    end as visit_type,
    case v.source when 0 then 'organic' when 1 then 'sync' when 2 then 'extension' when 3 then 'restore' end as source
from {{ source('browser_history', 'zen_historyvisits') }} v
join {{ source('browser_history', 'zen_places') }} p on p.id = v.place_id
left join {{ source('browser_history', 'zen_origins') }} o on o.id = p.origin_id
