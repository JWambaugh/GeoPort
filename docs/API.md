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
`source`, `updated_at`, or `null`), `accuracy_m`, `drive` (state, progress,
remaining seconds, speed, stop counts), `trip` (state, phase, detail, dwell
remaining, ETA), and the reconnect fields `reconnecting`,
`last_disconnect_at`, and `reconnect_attempts`.

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
./scripts/geoportctl drive --to "South Bank, Brisbane" --no-stops
```

`speed_offset_pct` is the same -40 to +40 (server clamp is wider) offset as
the Drive card. `start: false` only plans. `stops` defaults to true. Set
`"stops": false` to skip stop signs and signals. The plan's `stops` object
counts `stop_signs` and `signals`. `eta_s` adds the expected signal wait
(45% of signals, mean 25 s). Playback uses the rolled waits, so a red light
is a real stop of 5–45 s and a green light is only a slight slowdown.
`stop_markers` are the points drawn on the map. A long route is queried in
chunks of about 4 km. Mirrors are rotated (`overpass.openstreetmap.fr` was
live when this was written; `overpass-api.de`, `overpass.kumi.systems`,
`overpass.private.coffee`, and the Mail.ru endpoint stay in the list) with
a 25 second timeout and a backoff after HTTP 429, 5xx, or a timeout.
Planning waits about 3.5 seconds, then the drive can start. Chunks that are
still running finish in the background; stops still ahead of the car are
added to playback and `drive_plan` is sent again. `stops.lookup.state` is
`pending`, `partial`, `complete`, or `failed`, and `stops.lookup.chunks`
lists each piece. Failures are not cached. A successful chunk is stored in
`~/.geoport/stops_cache`, keyed by its bbox rounded to 0.01 degrees.

```bash
./scripts/geoportctl pause
./scripts/geoportctl resume
./scripts/geoportctl stop
./scripts/geoportctl start
```

After pause, stop, or arrival, `status.position` is the fix at that moment,
not the original start.

## trip

A trip is an ordered list of stops. The first leg starts at the current
simulated position unless `from` is set. Each stop is reached by `mode`
`drive` (default) or `walk`. `dwell_minutes` may be 0. `waypoint: true` is a
pass-through pin: no dwell and no walk inside. `go_inside` defaults to true
on a drive stop. On arrival the car stops at the road or parking point, then
the position walks off-road at about 1.3 m/s to the place, dwells there, and
walks back to the car before the next drive. A walk leg uses an OSRM foot
route, then Valhalla's pedestrian profile, then a straight line. Walks do
not stop for signs or signals. `walk_speed_mps` changes the pace (default
1.3). Long walks vary that pace slightly and may take one short pause.

Drive legs still use the speed-limit player, including stop signs and
signals unless `include_stops` is false. The plan's per-stop `travel_s`
includes the walk into the store, the walk back to the car, and the expected
signal delay. `arrive_at` and `depart_at` are clock times. `eta_s` is the
time until the last stop is finished.

```bash
curl -sS -H 'Content-Type: application/json' \
  -d '{"stops":[
        {"address":"Target, Grass Valley","dwell_minutes":20},
        {"lat":39.219,"lng":-121.061,"dwell_minutes":5,"mode":"walk","label":"Lot"},
        {"lat":39.220,"lng":-121.063,"dwell_minutes":0,"mode":"walk","waypoint":true},
        {"address":"Costco, Grass Valley","dwell_minutes":40,"go_inside":false}
      ],"include_stops":true,"speed_offset_pct":0,"walk_speed_mps":1.3}' \
  "$GEOPORT_URL/api/v1/trip"

curl -sS "$GEOPORT_URL/api/v1/trip"
./scripts/geoportctl trip --stop "Target, Grass Valley" 20 \
  --walk "39.219,-121.061" 5 --pin 39.220,-121.063 0 \
  --stop "Costco, Grass Valley" 40
./scripts/geoportctl trip --file trip.json --no-start
./scripts/geoportctl trip status
./scripts/geoportctl trip pause
./scripts/geoportctl trip resume
./scripts/geoportctl trip skip
./scripts/geoportctl trip stop
```

`start: false` only plans. Omit it, or send `start: true`, to leave
immediately. `--pin lat,lng 0` and `--walk … 0` are pass-through waypoints.
`--park` before a `--stop` waits in the car instead of walking inside.
`--walk-speed 1.4` sets the pace. The map UI calls the same handlers at
`/api/trip`, which is not limited to loopback.

Pause freezes the drive clock and the dwell clock. Skip finishes the current
drive, walk, or dwell and continues with the next step. Stop cancels
playback and keeps the stop list.

```bash
curl -sS -X PATCH -H 'Content-Type: application/json' \
  -d '{"stops":[{"lat":39.22,"lng":-121.06,"dwell_minutes":15,"label":"Target"}]}' \
  "$GEOPORT_URL/api/v1/trip/stops"
```

`POST` or `PATCH /api/v1/trip/stops` replaces the stops that are not finished
yet. Dwell, order, mode, and destination can change while the trip is
running. The current leg is replanned from the simulated position when its
destination changes.

Each stop keeps a stable `id`. Edit one stop with
`PATCH /api/v1/trip/stops/{id}` or `PATCH /api/v1/trip/stops/{n}` where `n`
is the 1-based pin number. A finished stop is rejected. A stop you have
already arrived at can still change its label and dwell. `DELETE` on the
same path removes it. `"move": "up"` or `"down"` reorders it among the stops
that are not finished.

```bash
curl -sS -X PATCH -H 'Content-Type: application/json' \
  -d '{"dwell_minutes":30,"mode":"walk","to":"Costco, Grass Valley"}' \
  "$GEOPORT_URL/api/v1/trip/stops/2"

./scripts/geoportctl trip edit 2 --dwell 30 --to "Costco, Grass Valley" --mode walk
./scripts/geoportctl trip edit 2 --label "Lot" --park --up
./scripts/geoportctl trip edit 2 --delete
```

The Trip card routes as you edit. `POST /api/v1/trip/preview` (and `/api/trip/preview`)
draws the legs without waiting for stop signs or signals. Each leg is cached
by its mode, endpoints, and go-inside choice, so an unchanged leg is not
asked of the router again. The server emits `trip_preview` after each leg,
and `trip` when the draft is saved. Sign and signal lookup still runs when
you Plan or Start. The map fits the trip unless you have panned in the last
few seconds.

The trip is stored in `~/.geoport/trip.json`. If it was playing or paused
when GeoPort stops, the next launch continues from the current simulated
position. A dropped device does not pause the trip clock; auto-reconnect
keeps retrying through drives and dwells. `status.trip` is a short summary:
state, phase, the "Dwelling at …" line, dwell remaining, and ETA.

Named trips live in `~/.geoport/trips.json`.

```bash
./scripts/geoportctl trip save errands
./scripts/geoportctl trip library
./scripts/geoportctl trip load errands
curl -sS -H 'Content-Type: application/json' \
  -d '{"name":"errands","start":false}' \
  "$GEOPORT_URL/api/v1/trip/library/load"
```

## accuracy

```bash
curl -sS "$GEOPORT_URL/api/v1/accuracy"
curl -sS -H 'Content-Type: application/json' -d '{"meters":25}' "$GEOPORT_URL/api/v1/accuracy"
./scripts/geoportctl accuracy
./scripts/geoportctl accuracy 25
```

`0` clears the wander. The setting is stored with the simulated position.

## connect

GeoPort connects on its own to the last device that worked (saved in
`~/.geoport/last_device.json`) or the only device it can see, preferring
USB and falling back to Wi-Fi. If the tunnel drops while a fix is set or a
drive is running, it retries with backoff and puts the current simulated
position back on the phone. The drive clock keeps running during the gap.
`POST /api/v1/connect` asks for that attempt immediately. Disconnect in the
UI pauses auto-connect until the next connect.

```bash
curl -sS -X POST "$GEOPORT_URL/api/v1/connect"
./scripts/geoportctl connect
```

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
