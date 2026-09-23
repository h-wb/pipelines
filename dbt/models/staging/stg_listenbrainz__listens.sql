select
    _dlt_id as listen_id,
    to_timestamp(listened_at) as started_at,
    -- Most listens have no length; assume an average track, cap outliers at 15 min
    to_timestamp(listened_at) + make_interval(secs => least(coalesce(
        track_metadata__additional_info__duration_ms / 1000.0,
        track_metadata__additional_info__duration,
        210), 900)) as ended_at,
    (to_timestamp(listened_at) at time zone '{{ var("timezone") }}')::date as day,
    extract(hour from to_timestamp(listened_at) at time zone '{{ var("timezone") }}')::int as hour,
    track_metadata__artist_name as artist,
    track_metadata__track_name as track,
    track_metadata__release_name as album
from {{ source('listenbrainz', 'listens') }}
