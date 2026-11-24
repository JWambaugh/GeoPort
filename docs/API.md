# GeoPort local automation API

The assistant API is for a script or AI on the same Mac as GeoPort. It is
accepted from **loopback only** (`127.0.0.1`, `::1`). Set
`GEOPORT_API_ALLOW_REMOTE=1` to allow other addresses. The map UI itself
still follows the server's normal bind address (`--host`, default
`0.0.0.0`).

Default base URL, when GeoPort is on its usual port:

```bash
export GEOPORT_URL=http://127.0.0.1:54321
```

There is a curl wrapper at `scripts/geoportctl`. It uses `GEOPORT_URL`.

The simulated position is the last fix GeoPort published (teleport, drive
tick, GPX, nudge, or accuracy wander). It is stored in
`~/.geoport/simulated_position.json` with the device it was applied to, and
it is what `status` reports as `position`. The map pin shows the same point.
`drive` with no `from` starts there, so "go to A" then "go to B" continues
from wherever the previous move stopped, paused, or finished.

iOS location simulation accepts only latitude and longitude
(`simulateLocationWithLatitude:longitude:`). There is no horizontal-accuracy
field. `accuracy_m` is a smooth wander around the fix (0 means exact, max
100). It applies while stationary and while a drive or GPX track is playing,
and it can be changed live.

Socket.IO events keep an open map in sync: `position` moves the pin,
`drive_plan` draws the route and fills the Drive card, `drive_tick` /
`drive_state` update progress.

## status

```bash
curl -sS "$GEOPORT_URL/api/v1/status"
./scripts/geoportctl status
```

`connected`, `simulating`, `device` (udid, name), `position` (`lat`, `lng`,
`source`, `updated_at`, or `null`), `accuracy_m`, and `drive` (state,
progress, remaining seconds, speed).

## teleport

```bash
curl -sS -H 'Content-Type: application/json' \
  -d '{"lat":-27.4705,"lng":153.026}' \
  "$GEOPORT_URL/api/v1/teleport"

curl -sS -H 'Content-Type: application/json' \
  -d '{"address":"Queen Street, Brisbane"}' \
  "$GEOPORT_URL/api/v1/teleport"

./scripts/geoportctl teleport --lat -27.4705 --lng 153.026
./scripts/geoportctl teleport --address "Brisbane CBD"
```

## geocode

```bash
curl -sS -G "$GEOPORT_URL/api/v1/geocode" --data-urlencode "q=South Bank, Brisbane"
./scripts/geoportctl geocode "South Bank, Brisbane"
```

Returns up to five `{lat, lng, display_name}` hits from Nominatim. The first
hit is what `teleport` and `drive` use when given an address.

## drive

Plans a driving route and, by default, starts it. Omit `from` to start at
the current simulated position.

```bash
curl -sS -H 'Content-Type: application/json' \
  -d '{"to":{"address":"South Bank, Brisbane"},"speed_offset_pct":10,"accuracy_m":15}' \
  "$GEOPORT_URL/api/v1/drive"

curl -sS -H 'Content-Type: application/json' \
  -d '{"to":{"lat":-27.48,"lng":153.03},"from":{"lat":-27.47,"lng":153.02},"start":false}' \
  "$GEOPORT_URL/api/v1/drive"

./scripts/geoportctl drive --to "South Bank, Brisbane" --offset 10 --accuracy 15
./scripts/geoportctl drive --to-lat -27.48 --to-lng 153.03 --from-lat -27.47 --from-lng 153.02 --no-start
```

`speed_offset_pct` is the same -40 to +40 (server clamp is wider) offset as
the Drive card. `start: false` only plans.

```bash
./scripts/geoportctl pause
./scripts/geoportctl resume
./scripts/geoportctl stop
./scripts/geoportctl start
```

After pause, stop, or arrival, `status.position` is the fix at that moment,
not the original start.

## accuracy

```bash
curl -sS "$GEOPORT_URL/api/v1/accuracy"
curl -sS -H 'Content-Type: application/json' -d '{"meters":25}' "$GEOPORT_URL/api/v1/accuracy"
./scripts/geoportctl accuracy
./scripts/geoportctl accuracy 25
```

`0` clears the wander. The setting is stored with the simulated position.

## devices and clear

```bash
curl -sS "$GEOPORT_URL/api/v1/devices"
./scripts/geoportctl devices

curl -sS -X POST "$GEOPORT_URL/api/v1/clear"
./scripts/geoportctl clear
```

`clear` stops any drive, forgets the simulated position, and clears GPS on a
connected phone. `devices` is the same discovery scan as the Devices list in
the UI.
