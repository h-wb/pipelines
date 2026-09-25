# Bike Share Toronto

Every Bike Share Toronto rental (stations, times, bike type).

- **Schedule**: daily 00:00 UTC (`load-bikeshare/load_bikeshare`)
- **Code**: [`src/sources/bikeshare.py`](../../src/sources/bikeshare.py), [`src/pipelines/bikeshare.py`](../../src/pipelines/bikeshare.py)
- **Raw schema**: `bikeshare_data.trips` (+ `trips__start_location`, `trips__end_location`)
- **dbt**: `staging.stg_bikeshare__trips` → `biking.{biking_rides, stations, routes}`, `crossovers.rides`
- **Dashboards**: Biking (3), Rides (26)

## Where the data comes from

The Bike Share mobile app's own API (`layer.bicyclesharing.net/mobile/v1/tor`,
`rental/closed`), called with the member id and auth token the app uses, and the
app's request headers. There's no public API for rental history.

## Extraction

One resource, `trips`: `rental/closed` for the member, incremental on `sd`
(rental start), key `rentalId`. The first run asks for 500 rentals per page,
later runs 50 (only the newest matter).

## Secrets and config

`BIKESHARE__MEMBER_ID`, `BIKESHARE__AUTHORIZATION_TOKEN` (Proton Pass
`dlt/bikeshare`), both taken from the mobile app's traffic.

## Quirks

- TLS verification is disabled for this host (the app's endpoint), and the
  request mimics the app's `User-Agent`.
- If the app token rotates or expires, runs fail with 401/403: capture a new one.
- Bike Share only estimates distance; `biking_rides` computes straight-line
  distance and speed from station coordinates.
