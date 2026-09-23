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
    track_metadata__release_name as album,
    case
        when track_metadata__additional_info__submission_client = 'ListenBrainz lastfm importer v2' then 'Last.fm (imported)'
        when coalesce(track_metadata__additional_info__music_service_name, track_metadata__additional_info__music_service) ilike '%spotify%' then 'Spotify'
        when coalesce(track_metadata__additional_info__music_service_name, track_metadata__additional_info__music_service) ilike '%youtube%' then 'YouTube'
        when track_metadata__additional_info__music_service_name = 'Plex' then 'Plex'
        else 'Other'
    end as service,
    track_metadata__mbid_mapping__recording_mbid as recording_mbid,
    track_metadata__mbid_mapping__caa_release_mbid as cover_release_mbid
from {{ source('listenbrainz', 'listens') }}
