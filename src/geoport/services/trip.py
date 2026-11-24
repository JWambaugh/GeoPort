"""Multi-stop trips on top of the drive engine.

A trip is an ordered list of stops. Each leg is either a drive, played by
:class:`geoport.services.drive.DriveService`, or a walk at about 1.3 m/s.
Driving reuses road routing, speed limits, and stop-sign playback. Walking
never asks for stop signs. "Go inside" is a short off-road walk from the
road endpoint to the place, then a dwell, then a walk back to the car.
"""
from __future__ import annotations

import json
import os
import random
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import requests
from flask import jsonify, request

from geoport.config import get_geoport_state_dir, logger
from geoport.core.models import Coordinate
from geoport.services.control import api_access_allowed, resolve_place
from geoport.services.drive import (
    USER_AGENT,
    DrivePlan,
    DriveRouteError,
    MicroEdge,
    Phase,
    _as_bool,
    build_profile,
    decode_polyline,
    fetch_segments,
    haversine_m,
    parse_osrm_route,
    position_at,
)

WALK_MPS = 1.3
WALK_VARIATION = 0.08
WALK_MIN_MPS = 0.5
WALK_MAX_MPS = 3.0
INSIDE_GAP_M = 8.0
CAR_GAP_M = 12.0
SHORT_STRAIGHT_M = 150.0
INDOOR_ACCURACY_M = 12.0
PAUSE_MIN_M = 40.0
_HTTP_TIMEOUT = (3.05, 12)
MOVEMENT = frozenset({'drive', 'walk', 'walk_in', 'walk_to_car'})

OSRM_FOOT_PRIMARY = os.getenv(
    'GEOPORT_OSRM_FOOT_URL',
    'https://routing.openstreetmap.de/routed-foot/route/v1/foot/',
)
OSRM_FOOT_FALLBACK = os.getenv(
    'GEOPORT_OSRM_FOOT_FALLBACK_URL',
    'https://router.project-osrm.org/route/v1/foot/',
)
VALHALLA_ROUTE_URL = os.getenv(
    'GEOPORT_VALHALLA_ROUTE_URL',
    'https://valhalla1.openstreetmap.de/route',
)


def reconnect_drive_state(drive_state: str, trip_state: str) -> str:
    """Keep auto-reconnect armed for the whole trip, including a dwell.

    The drive player is idle while someone is inside a store. The trip clock
    is not paused for a dropped device; this only reports the trip as active
    so the watchdog still retries.
    """
    if trip_state in ('playing', 'paused'):
        return trip_state
    return drive_state


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fmt_clock(seconds: float) -> str:
    whole = max(0, int(round(seconds)))
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{minutes}:{secs:02d}'


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _straight_points(
    start: tuple[float, float],
    end: tuple[float, float],
    step_m: float = 20.0,
) -> list[tuple[float, float]]:
    distance = haversine_m(start[0], start[1], end[0], end[1])
    if distance < 0.5:
        return [start, end]
    steps = max(1, int(distance // step_m))
    points = []
    for index in range(steps + 1):
        frac = index / steps
        points.append((
            start[0] + (end[0] - start[0]) * frac,
            start[1] + (end[1] - start[1]) * frac,
        ))
    return points


def _pin_ends(
    points: list[tuple[float, float]],
    start: tuple[float, float],
    end: tuple[float, float],
) -> list[tuple[float, float]]:
    if not points:
        return [start, end]
    pinned = list(points)
    if haversine_m(start[0], start[1], pinned[0][0], pinned[0][1]) >= 3.0:
        pinned.insert(0, start)
    if haversine_m(end[0], end[1], pinned[-1][0], pinned[-1][1]) >= 3.0:
        pinned.append(end)
    return pinned


def _valhalla_shape(payload: dict[str, Any]) -> list[tuple[float, float]]:
    trip = payload.get('trip') if isinstance(payload, dict) else None
    if not isinstance(trip, dict):
        trip = payload if isinstance(payload, dict) else {}
    points: list[tuple[float, float]] = []
    for leg in trip.get('legs') or []:
        shape = leg.get('shape')
        if isinstance(shape, dict):
            for coord in shape.get('coordinates') or []:
                if isinstance(coord, (list, tuple)) and len(coord) >= 2:
                    points.append((float(coord[1]), float(coord[0])))
        elif isinstance(shape, str) and shape:
            points.extend(decode_polyline(shape, precision=6))
    return points


def fetch_walk_points(
    start: tuple[float, float],
    end: tuple[float, float],
    http: Any,
) -> tuple[list[tuple[float, float]], str]:
    """Foot route from OSRM, then Valhalla pedestrian, then a straight line."""
    errors: list[str] = []
    for base in (OSRM_FOOT_PRIMARY, OSRM_FOOT_FALLBACK):
        if not base:
            continue
        url = f'{base.rstrip("/")}/{start[1]},{start[0]};{end[1]},{end[0]}'
        try:
            response = http.get(
                url,
                params={'overview': 'full', 'geometries': 'geojson', 'steps': 'false'},
                timeout=_HTTP_TIMEOUT,
                headers={'User-Agent': USER_AGENT},
            )
            response.raise_for_status()
            segments = parse_osrm_route(response.json())
            points = [(segments[0].lat1, segments[0].lng1)]
            for segment in segments:
                points.append((segment.lat2, segment.lng2))
            return _pin_ends(points, start, end), 'osrm-foot'
        except Exception as exc:
            errors.append(str(exc))
            logger.info(f'OSRM foot request failed ({base}): {exc}')
    try:
        response = http.post(
            VALHALLA_ROUTE_URL,
            json={
                'locations': [
                    {'lat': start[0], 'lon': start[1]},
                    {'lat': end[0], 'lon': end[1]},
                ],
                'costing': 'pedestrian',
                'shape_format': 'geojson',
            },
            timeout=_HTTP_TIMEOUT,
            headers={'User-Agent': USER_AGENT, 'Content-Type': 'application/json'},
        )
        response.raise_for_status()
        points = _valhalla_shape(response.json())
        if len(points) >= 2:
            return _pin_ends(points, start, end), 'valhalla-pedestrian'
        errors.append('Valhalla pedestrian shape was empty')
    except Exception as exc:
        errors.append(str(exc))
        logger.info(f'Valhalla pedestrian request failed: {exc}')
    logger.info('Walking route fell back to a straight line: ' + '; '.join(errors))
    return _straight_points(start, end), 'straight'


def build_walk_plan(
    points: list[tuple[float, float]],
    speed_mps: float = WALK_MPS,
    rng: Optional[random.Random] = None,
    variation: float = WALK_VARIATION,
    allow_pause: bool = True,
    provider: str = 'walk',
) -> Optional[DrivePlan]:
    """Constant walking pace along ``points``, with a little speed variation.

    Stop signs are not applied. A walk longer than about 40 m may include
    one short pause. The result is a :class:`DrivePlan` so the existing
    drive player can move the pin.
    """
    speed_mps = min(WALK_MAX_MPS, max(WALK_MIN_MPS, float(speed_mps)))
    segments: list[MicroEdge] = []
    phases: list[Phase] = []
    clock = 0.0
    distance = 0.0
    raw: list[tuple[float, float, float, float, float]] = []
    for start, end in zip(points, points[1:]):
        length = haversine_m(start[0], start[1], end[0], end[1])
        if length < 0.4:
            continue
        factor = 1.0
        if variation and rng is not None:
            factor = 1.0 + rng.uniform(-abs(variation), abs(variation))
        pace = min(WALK_MAX_MPS, max(WALK_MIN_MPS, speed_mps * factor))
        raw.append((start[0], start[1], end[0], end[1], length))
        segment = MicroEdge(
            lat1=start[0], lng1=start[1], lat2=end[0], lng2=end[1],
            length_m=length, limit_kmh=pace * 3.6, source='walk',
        )
        segment.d0 = distance
        segment.d1 = distance + length
        dt = length / pace
        phases.append(Phase(
            t0=clock, t1=clock + dt, d0=distance, d1=distance + length,
            v0=pace, v1=pace, segment_index=len(segments),
        ))
        clock += dt
        distance += length
        segments.append(segment)
    if not segments:
        return None
    if allow_pause and distance >= PAUSE_MIN_M and rng is not None and rng.random() < 0.65:
        pause_s = 0.8 + rng.random() * 1.2
        mid = distance / 2.0
        inserted: list[Phase] = []
        shifted = False
        extra = 0.0
        for phase in phases:
            if not shifted and phase.d0 <= mid <= phase.d1:
                cut_t = phase.t0 + extra
                span = phase.t1 - phase.t0
                frac = 0.0 if phase.d1 <= phase.d0 else (mid - phase.d0) / (phase.d1 - phase.d0)
                split_t = phase.t0 + span * frac
                if split_t - phase.t0 > 0.2:
                    inserted.append(Phase(
                        t0=cut_t, t1=cut_t + (split_t - phase.t0),
                        d0=phase.d0, d1=mid, v0=phase.v0, v1=phase.v0,
                        segment_index=phase.segment_index,
                    ))
                    cut_t = inserted[-1].t1
                inserted.append(Phase(
                    t0=cut_t, t1=cut_t + pause_s, d0=mid, d1=mid,
                    v0=0.0, v1=0.0, segment_index=phase.segment_index,
                ))
                cut_t += pause_s
                if phase.t1 - split_t > 0.2:
                    inserted.append(Phase(
                        t0=cut_t, t1=cut_t + (phase.t1 - split_t),
                        d0=mid, d1=phase.d1, v0=phase.v0, v1=phase.v0,
                        segment_index=phase.segment_index,
                    ))
                extra += pause_s
                shifted = True
            else:
                inserted.append(Phase(
                    t0=phase.t0 + extra, t1=phase.t1 + extra,
                    d0=phase.d0, d1=phase.d1, v0=phase.v0, v1=phase.v1,
                    segment_index=phase.segment_index,
                ))
        phases = inserted
        clock += pause_s
    geometry = [[segments[0].lat1, segments[0].lng1]]
    for segment in segments:
        geometry.append([segment.lat2, segment.lng2])
    return DrivePlan(
        segments=segments,
        phases=phases,
        distance_m=distance,
        duration_s=clock,
        provider=provider,
        speed_offset_pct=0.0,
        geometry=geometry,
        stops=[],
        stops_summary={
            'stop_signs': 0,
            'signals': 0,
            'available': True,
            'note': 'Walking skips stop signs',
            'expected_signal_s': 0,
            'realized_signal_s': 0,
        },
        eta_s=clock,
    )


@dataclass
class TripStop:
    id: str
    lat: float
    lng: float
    label: str
    mode: str
    dwell_s: float
    go_inside: bool
    waypoint: bool
    address: Optional[str] = None
    road_lat: Optional[float] = None
    road_lng: Optional[float] = None
    parts: list[dict[str, Any]] = field(default_factory=list)
    distance_m: float = 0.0
    travel_s: float = 0.0
    drive_s: float = 0.0
    walk_s: float = 0.0
    walk_in_s: float = 0.0
    walk_back_s: float = 0.0
    stop_signs: int = 0
    signals: int = 0
    expected_signal_s: float = 0.0
    arrived_at: Optional[str] = None
    departed_at: Optional[str] = None

    @property
    def dwell_minutes(self) -> float:
        return round(self.dwell_s / 60.0, 2)

    def to_input(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'id': self.id,
            'lat': self.lat,
            'lng': self.lng,
            'label': self.label,
            'mode': self.mode,
            'dwell_minutes': self.dwell_minutes,
            'go_inside': self.go_inside,
            'waypoint': self.waypoint,
        }
        if self.address:
            payload['address'] = self.address
        return payload

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_input()
        payload.update({
            'dwell_s': round(self.dwell_s, 1),
            'road': None if self.road_lat is None else {'lat': self.road_lat, 'lng': self.road_lng},
            'parts': self.parts,
            'distance_m': round(self.distance_m, 1),
            'travel_s': round(self.travel_s, 1),
            'drive_s': round(self.drive_s, 1),
            'walk_s': round(self.walk_s, 1),
            'walk_in_s': round(self.walk_in_s, 1),
            'walk_back_s': round(self.walk_back_s, 1),
            'stop_signs': self.stop_signs,
            'signals': self.signals,
            'expected_signal_s': round(self.expected_signal_s, 1),
            'arrived_at': self.arrived_at,
            'departed_at': self.departed_at,
        })
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> 'TripStop':
        road = raw.get('road') or {}
        return cls(
            id=str(raw.get('id') or _new_id()),
            lat=float(raw['lat']),
            lng=float(raw['lng']),
            label=str(raw.get('label') or ''),
            mode='walk' if raw.get('mode') == 'walk' else 'drive',
            dwell_s=float(raw.get('dwell_s') or 0),
            go_inside=bool(raw.get('go_inside')),
            waypoint=bool(raw.get('waypoint')),
            address=raw.get('address'),
            road_lat=road.get('lat') if isinstance(road, dict) else None,
            road_lng=road.get('lng') if isinstance(road, dict) else None,
            parts=list(raw.get('parts') or []),
            distance_m=float(raw.get('distance_m') or 0),
            travel_s=float(raw.get('travel_s') or 0),
            drive_s=float(raw.get('drive_s') or 0),
            walk_s=float(raw.get('walk_s') or 0),
            walk_in_s=float(raw.get('walk_in_s') or 0),
            walk_back_s=float(raw.get('walk_back_s') or 0),
            stop_signs=int(raw.get('stop_signs') or 0),
            signals=int(raw.get('signals') or 0),
            expected_signal_s=float(raw.get('expected_signal_s') or 0),
            arrived_at=raw.get('arrived_at'),
            departed_at=raw.get('departed_at'),
        )


def _same_place(a: TripStop, b: TripStop, meters: float = 8.0) -> bool:
    return (
        a.mode == b.mode
        and haversine_m(a.lat, a.lng, b.lat, b.lng) <= meters
    )


class TripService:
    """Plan and play an ordered itinerary. One trip at a time."""

    def __init__(
        self,
        drive: Any,
        position: Any = None,
        http: Any = None,
        path: Optional[Path] = None,
        library_path: Optional[Path] = None,
        emit: Optional[Callable[[str, dict[str, Any]], None]] = None,
        geocoder: Optional[Callable[[str], list[dict[str, Any]]]] = None,
        drive_router: Optional[Callable[..., tuple[DrivePlan, tuple[float, float]]]] = None,
        walk_router: Optional[Callable[..., tuple[list[tuple[float, float]], str]]] = None,
        now: Optional[Callable[[], float]] = None,
        manual: bool = False,
    ) -> None:
        self.drive = drive
        self.position = position
        self._http = http if http is not None else requests.Session()
        self.path = Path(path) if path is not None else get_geoport_state_dir() / 'trip.json'
        self.library_path = Path(library_path) if library_path is not None else get_geoport_state_dir() / 'trips.json'
        self._emit_fn = emit
        self._geocoder = geocoder or self._default_geocoder
        self._drive_router = drive_router or self._default_drive_router
        self._walk_router = walk_router or self._default_walk_router
        self._now = now or __import__('time').monotonic
        self._manual = manual
        self._lock = threading.Lock()
        self._step_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.owns_drive = False
        self.state = 'idle'
        self.phase = 'idle'
        self.index = 0
        self.stops: list[TripStop] = []
        self.origin: Optional[tuple[float, float]] = None
        self.include_stops = True
        self.offset = 0.0
        self.walk_speed = WALK_MPS
        self.walk_variation = WALK_VARIATION
        self.provider = 'osrm'
        self.google_key = ''
        self._stops_seed: Optional[int] = None
        self._queue: list[str] = []
        self._car: Optional[tuple[float, float]] = None
        self._in_car = True
        self._active_plan: Optional[DrivePlan] = None
        self._awaiting = False
        self._ignore_drive = False
        self._token = 0
        self._dwell_total = 0.0
        self._dwell_elapsed = 0.0
        self._dwell_mark = 0.0
        self._accuracy_saved: Optional[float] = None
        self._error: Optional[str] = None
        self._rng = random.Random()
        self._last_save = 0.0
        self._closed = False
        if not manual:
            self._thread = threading.Thread(target=self._loop, name='geoport-trip', daemon=True)
            self._thread.start()

    @property
    def active(self) -> bool:
        return self.state in ('playing', 'paused')

    def _default_geocoder(self, query: str) -> list[dict[str, Any]]:
        from geoport.services.control import geocode_address
        return geocode_address(query, http=self._http)

    def _default_walk_router(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> tuple[list[tuple[float, float]], str]:
        return fetch_walk_points(start, end, self._http)

    def _default_drive_router(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        include_stops: bool,
        offset_pct: float,
    ) -> tuple[DrivePlan, tuple[float, float]]:
        segments, provider = fetch_segments(
            start,
            end,
            self.provider,
            self.google_key,
            self._http,
            anchor_end=False,
        )
        road = (segments[-1].lat2, segments[-1].lng2)
        planned: list[Any] = []
        summary: dict[str, Any] = {
            'stop_signs': 0,
            'signals': 0,
            'available': not include_stops,
            'note': None if include_stops else 'Stops disabled',
            'expected_signal_s': 0,
            'realized_signal_s': 0,
        }
        if include_stops:
            planned, summary = self._preview_stops(segments)
        plan = build_profile(
            segments,
            offset_pct,
            provider=provider,
            stops=planned,
            stops_summary=summary,
        )
        return plan, road

    def _preview_stops(self, segments: list[MicroEdge]) -> tuple[list[Any], dict[str, Any]]:
        from geoport.services.stops import PLAN_WAIT_S, StopLookup, assign_dwells, summarize_stops
        lookup = StopLookup(segments, self._http, get_geoport_state_dir() / 'stops_cache')
        lookup.start()
        snapped, state = lookup.wait(PLAN_WAIT_S)
        lookup.cancel()
        lookup.join(0.3)
        rng = random.Random(self._stops_seed) if self._stops_seed is not None else random.Random()
        planned = assign_dwells(snapped, rng)
        summary = summarize_stops(planned, state.get('state') != 'failed', state.get('note'))
        summary['lookup'] = state
        return planned, summary

    def plan_body(self, body: dict[str, Any]) -> dict[str, Any]:
        raw = body.get('stops')
        if isinstance(raw, dict):
            raise DriveRouteError('stops must be a list of places')
        if not isinstance(raw, list) or not raw:
            raise DriveRouteError('stops must be a non-empty list')
        if self.active:
            self.stop(user=True)
        self._apply_options(body)
        origin = self._origin_from_body(body)
        resolved = [self._resolve(item) for item in raw]
        self.stops = resolved
        self.origin = origin
        self.index = 0
        self.phase = 'idle'
        self.state = 'idle'
        self._queue = []
        self._car = origin
        self._in_car = True
        self._error = None
        self._awaiting = False
        seed = body.get('walk_seed')
        self._rng = random.Random() if seed is None else random.Random(int(seed))
        try:
            self._build_preview(origin)
        except Exception:
            self.stops = []
            raise
        self._persist(force=True)
        do_start = True if body.get('start') is None else _as_bool(body.get('start'), True)
        if do_start:
            return self.start()
        self._emit()
        return self.status()

    def start(self) -> dict[str, Any]:
        if not self.stops:
            raise DriveRouteError('Add at least one stop')
        if self.state == 'paused':
            return self.resume()
        if self.state == 'playing':
            return self.status()
        person = self._person()
        self.state = 'playing'
        self._error = None
        self.index = 0
        for stop in self.stops:
            stop.arrived_at = None
            stop.departed_at = None
        try:
            if self.origin is None or haversine_m(person[0], person[1], self.origin[0], self.origin[1]) > 20:
                self.origin = person
                self._car = person
                self._in_car = True
                self._build_preview(person)
            elif self._car is None:
                self._car = person
                self._in_car = True
            with self._step_lock:
                self._begin_stop(0)
        except Exception:
            self.state = 'idle'
            self.phase = 'idle'
            self._awaiting = False
            self.owns_drive = False
            self._silence_drive_stop()
            self._persist(force=True)
            self._emit()
            raise
        self._persist(force=True)
        self._emit()
        return self.status()

    def pause(self) -> dict[str, Any]:
        if self.state != 'playing':
            raise DriveRouteError('Trip is not running')
        with self._step_lock:
            if self.phase == 'dwell':
                self._dwell_elapsed = self._dwell_done()
            self.state = 'paused'
            if self.phase in MOVEMENT:
                self.drive.pause()
        self._persist(force=True)
        self._emit()
        return self.status()

    def resume(self) -> dict[str, Any]:
        if self.state != 'paused':
            raise DriveRouteError('Trip is not paused')
        with self._step_lock:
            self.state = 'playing'
            self._error = None
            if self.phase == 'dwell':
                self._dwell_mark = self._now()
            elif self.phase in MOVEMENT:
                try:
                    self.drive.resume()
                except DriveRouteError:
                    self._restart_phase()
        self._persist(force=True)
        self._emit()
        return self.status()

    def skip(self) -> dict[str, Any]:
        if self.state not in ('playing', 'paused'):
            raise DriveRouteError('No trip is running')
        with self._step_lock:
            self.state = 'playing'
            self._token += 1
            phase = self.phase
            plan = self._active_plan
            self._awaiting = False
            if phase in MOVEMENT:
                self._silence_drive_stop()
                if plan is not None:
                    sample = position_at(plan, plan.duration_s)
                    self._apply_arrival(phase, sample.lat, sample.lng)
            elif phase == 'dwell':
                self._leave_indoor()
            else:
                raise DriveRouteError('Nothing to skip')
            if self._queue and self._queue[0] == phase:
                self._queue.pop(0)
            self._begin_after_phase()
        self._persist(force=True)
        self._emit()
        return self.status()

    def stop(self, user: bool = True) -> dict[str, Any]:
        del user
        with self._step_lock:
            self._token += 1
            self._awaiting = False
            self.owns_drive = False
            self._leave_indoor()
            self._silence_drive_stop()
            self.state = 'idle'
            self.phase = 'idle'
            self._queue = []
            self.index = 0
            self._error = None
        self._persist(force=True)
        self._emit()
        return self.status()

    def replace_stops(self, raw_stops: list[Any]) -> dict[str, Any]:
        if not isinstance(raw_stops, list) or not raw_stops:
            raise DriveRouteError('stops must be a non-empty list')
        resolved = [self._resolve(item) for item in raw_stops]
        if not self.active:
            origin = self._person() if self.position is not None and self._has_fix() else self.origin
            if origin is None:
                raise DriveRouteError('No simulated location yet. Pass from, or simulate a position first.')
            self.stops = resolved
            self.origin = origin
            self.index = 0
            self.state = 'idle'
            self.phase = 'idle'
            self._car = origin
            self._in_car = True
            self._build_preview(origin)
            self._persist(force=True)
            self._emit()
            return self.status()
        with self._step_lock:
            done = self.stops[:self.index]
            rest = self._remaining_from(resolved, done)
            if not rest:
                raise DriveRouteError('The current stop has to stay until you leave it')
            current = self.stops[self.index]
            new_current = rest[0]
            same = _same_place(current, new_current) and current.mode == new_current.mode
            if same:
                new_current.id = current.id
                new_current.road_lat = current.road_lat
                new_current.road_lng = current.road_lng
                new_current.arrived_at = current.arrived_at
                new_current.parts = current.parts
                new_current.distance_m = current.distance_m
                new_current.travel_s = current.travel_s
                new_current.drive_s = current.drive_s
                new_current.walk_s = current.walk_s
                new_current.walk_in_s = current.walk_in_s
                new_current.walk_back_s = current.walk_back_s
                new_current.stop_signs = current.stop_signs
                new_current.signals = current.signals
                new_current.expected_signal_s = current.expected_signal_s
            self.stops = done + rest
            if same and self.phase == 'dwell':
                self.stops[self.index].dwell_s = 0.0 if new_current.waypoint else new_current.dwell_s
                self.stops[self.index].go_inside = new_current.go_inside
                self.stops[self.index].waypoint = new_current.waypoint
                self._dwell_total = self.stops[self.index].dwell_s
                self._refill_future()
                if self._dwell_done() + 1e-3 >= self._dwell_total:
                    self._leave_indoor()
                    if self._queue and self._queue[0] == 'dwell':
                        self._queue.pop(0)
                    self._begin_after_phase()
            elif same:
                self.stops[self.index].dwell_s = 0.0 if new_current.waypoint else new_current.dwell_s
                self.stops[self.index].go_inside = new_current.go_inside
                self.stops[self.index].waypoint = new_current.waypoint
                self.stops[self.index].label = new_current.label
                if new_current.waypoint and 'dwell' in self._queue:
                    self._queue = [phase for phase in self._queue if phase != 'dwell']
                elif self.stops[self.index].dwell_s > 0 and 'dwell' not in self._queue and 'walk_in' not in self._queue:
                    if self.phase != 'dwell':
                        self._queue.append('dwell')
                self._refill_future()
            else:
                self._restart_toward_current()
        self._persist(force=True)
        self._emit()
        return self.status()

    def note_drive_state(self, state: str, user_stop: bool = False) -> None:
        if self._ignore_drive:
            return
        if state == 'idle' and user_stop and self.active:
            self._cancel_from_drive()
            return
        if state == 'paused' and self.state == 'playing' and self.phase in MOVEMENT:
            if self.phase == 'dwell':
                self._dwell_elapsed = self._dwell_done()
            self.state = 'paused'
            self._persist(force=True)
            self._emit()
            return
        if state == 'playing' and self.state == 'paused' and self.phase in MOVEMENT:
            self.state = 'playing'
            self._persist(force=True)
            self._emit()
            return
        if state != 'finished' or not self._awaiting or self.state != 'playing':
            return
        token = self._token
        self._awaiting = False
        if self._manual:
            self._complete_movement(token)
        else:
            threading.Thread(
                target=self._complete_movement,
                args=(token,),
                name='geoport-trip-step',
                daemon=True,
            ).start()

    def tick(self) -> None:
        if self._closed or self.state != 'playing' or self.phase != 'dwell':
            if self.active and self._now() - self._last_save > 5:
                self._persist()
            return
        if self._dwell_done() + 1e-3 < self._dwell_total:
            if self._now() - self._last_save > 5:
                self._persist()
            return
        if not self._step_lock.acquire(blocking=False):
            return
        try:
            if self.state != 'playing' or self.phase != 'dwell':
                return
            if self._dwell_done() + 1e-3 < self._dwell_total:
                return
            self._leave_indoor()
            if self._queue and self._queue[0] == 'dwell':
                self._queue.pop(0)
            self._begin_after_phase()
        finally:
            self._step_lock.release()
        self._persist(force=True)
        self._emit()

    def status(self) -> dict[str, Any]:
        with self._lock:
            stops = list(self.stops)
            state = self.state
            phase = self.phase
            index = self.index
            queue = list(self._queue)
            error = self._error
            include_stops = self.include_stops
            offset = self.offset
            walk_speed = self.walk_speed
        times = self._clock_times(stops, state, phase, index, queue)
        public_stops = []
        for stop_index, stop in enumerate(stops):
            row = stop.to_dict()
            if state in ('playing', 'paused') and stop_index < index:
                row['status'] = 'done'
            elif state in ('playing', 'paused') and stop_index == index:
                row['status'] = 'current'
            elif state == 'finished':
                row['status'] = 'done'
            else:
                row['status'] = 'upcoming'
            arrive, depart = times[stop_index] if stop_index < len(times) else (None, None)
            row['arrive_at'] = stop.arrived_at or arrive
            row['depart_at'] = stop.departed_at or depart
            public_stops.append(row)
        remaining = self._remaining_seconds(stops, state, phase, index, queue)
        total = sum(stop.travel_s + (0.0 if stop.waypoint else stop.dwell_s) for stop in stops)
        current = stops[index] if state in ('playing', 'paused') and index < len(stops) else None
        nxt = stops[index + 1] if current is not None and index + 1 < len(stops) else None
        if state == 'idle' and stops:
            current_label = None
            next_label = stops[0].label
        else:
            current_label = current.label if current is not None else None
            next_label = nxt.label if nxt is not None else None
        dwell_left = 0.0
        if phase == 'dwell':
            dwell_left = max(0.0, self._dwell_total - self._dwell_done())
        finish_at = None
        if stops and times:
            finish_at = times[-1][1]
        if state in ('playing', 'paused') and remaining >= 0:
            finish_at = (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=remaining)).isoformat()
        return {
            'ok': True,
            'state': state,
            'phase': phase,
            'detail': self._detail(state, phase, current_label, dwell_left),
            'stop_index': index if state in ('playing', 'paused') else 0,
            'stop_count': len(stops),
            'current_label': current_label,
            'next_label': next_label,
            'current_stop': None if current is None else public_stops[index],
            'next_stop': None if nxt is None else public_stops[index + 1],
            'dwell_remaining_s': round(dwell_left, 1),
            'leg_remaining_s': round(self._phase_remaining() if state in ('playing', 'paused') else 0.0, 1),
            'eta_s': round(max(0.0, remaining), 1),
            'finish_at': finish_at,
            'total_distance_m': round(sum(stop.distance_m for stop in stops), 1),
            'total_duration_s': round(total, 1),
            'progress': 0.0 if total <= 0 else round(min(1.0, max(0.0, 1.0 - remaining / total)), 4),
            'include_stops': include_stops,
            'speed_offset_pct': offset,
            'walk_speed_mps': walk_speed,
            'error': error,
            'stops': public_stops,
            'active': state in ('playing', 'paused'),
        }

    def summary(self) -> dict[str, Any]:
        full = self.status()
        return {
            'state': full['state'],
            'phase': full['phase'],
            'detail': full['detail'],
            'stop_index': full['stop_index'],
            'stop_count': full['stop_count'],
            'dwell_remaining_s': full['dwell_remaining_s'],
            'eta_s': full['eta_s'],
            'finish_at': full['finish_at'],
            'current_label': full['current_label'],
            'next_label': full['next_label'],
            'active': full['active'],
            'total_distance_m': full['total_distance_m'],
            'total_duration_s': full['total_duration_s'],
            'progress': full['progress'],
        }

    def library(self) -> dict[str, Any]:
        saved = self._read_library()
        names = [
            {'name': name, 'saved_at': item.get('saved_at'), 'stops': len(item.get('stops') or [])}
            for name, item in sorted(saved.items())
        ]
        return {'ok': True, 'trips': names}

    def save_named(self, name: str) -> dict[str, Any]:
        cleaned = _clean_name(name)
        if not self.stops:
            raise DriveRouteError('Plan a trip before saving it')
        saved = self._read_library()
        saved[cleaned] = {
            'saved_at': _now_iso(),
            'stops': [stop.to_input() for stop in self.stops],
            'include_stops': self.include_stops,
            'speed_offset_pct': self.offset,
            'walk_speed_mps': self.walk_speed,
        }
        self._write_library(saved)
        return {'ok': True, 'name': cleaned, 'stops': len(self.stops)}

    def load_named(self, name: str, start: bool = False) -> dict[str, Any]:
        cleaned = _clean_name(name)
        saved = self._read_library()
        item = saved.get(cleaned)
        if not isinstance(item, dict):
            raise DriveRouteError(f'No saved trip named {cleaned}', status=404)
        body = {
            'stops': item.get('stops') or [],
            'include_stops': item.get('include_stops', True),
            'speed_offset_pct': item.get('speed_offset_pct', 0),
            'walk_speed_mps': item.get('walk_speed_mps', WALK_MPS),
            'start': start,
        }
        result = self.plan_body(body)
        result['loaded'] = cleaned
        return result

    def delete_named(self, name: str) -> dict[str, Any]:
        cleaned = _clean_name(name)
        saved = self._read_library()
        if cleaned not in saved:
            raise DriveRouteError(f'No saved trip named {cleaned}', status=404)
        del saved[cleaned]
        self._write_library(saved)
        return {'ok': True, 'name': cleaned}

    def maybe_resume(self) -> None:
        data = self._read_persisted()
        if not data:
            return
        try:
            self._restore(data)
        except Exception as exc:
            logger.warning(f'Could not restore the trip: {exc}')
            return
        if self.state == 'playing' and self.phase in MOVEMENT:
            try:
                with self._step_lock:
                    self._restart_phase()
            except Exception as exc:
                logger.warning(f'Trip resume failed: {exc}')
                self.state = 'paused'
                self._error = str(exc)
        elif self.state == 'playing' and self.phase == 'dwell':
            self._dwell_mark = self._now()
            stop = self.stops[self.index] if self.index < len(self.stops) else None
            if stop is not None and stop.go_inside:
                self._enter_indoor()
        self._persist(force=True)
        self._emit()

    def close(self) -> None:
        if self.phase == 'dwell' and self.state == 'playing':
            self._dwell_elapsed = self._dwell_done()
            self._dwell_mark = self._now()
        self._closed = True
        self._stop_event.set()
        self._persist(force=True)
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)

    def _loop(self) -> None:
        last_emit = 0.0
        while not self._stop_event.wait(0.25):
            try:
                self.tick()
                now = self._now()
                if self.active and now - last_emit >= 1.0:
                    self._emit()
                    last_emit = now
            except Exception as exc:
                logger.debug(f'Trip tick failed: {exc}')

    def _apply_options(self, body: dict[str, Any]) -> None:
        if 'include_stops' in body:
            self.include_stops = _as_bool(body.get('include_stops'), True)
        elif 'signs' in body:
            self.include_stops = _as_bool(body.get('signs'), True)
        if body.get('speed_offset_pct') is not None:
            self.offset = float(body.get('speed_offset_pct') or 0)
        if body.get('walk_speed_mps') is not None:
            self.walk_speed = min(WALK_MAX_MPS, max(WALK_MIN_MPS, float(body['walk_speed_mps'])))
        if body.get('walk_variation') is not None:
            self.walk_variation = max(0.0, min(0.3, float(body['walk_variation'])))
        if body.get('provider'):
            self.provider = str(body.get('provider'))
        if body.get('stops_seed') not in (None, ''):
            self._stops_seed = int(body['stops_seed'])
        key = body.get('google_api_key') or ''
        if isinstance(key, str) and key.strip():
            self.google_key = key.strip()
        else:
            self.google_key = os.getenv('GEOPORT_GOOGLE_MAPS_KEY', '').strip()

    def _origin_from_body(self, body: dict[str, Any]) -> tuple[float, float]:
        origin = body.get('from') if body.get('from') is not None else body.get('start_at')
        if origin is not None:
            lat, lng, _label = resolve_place(origin, self._geocoder)
            return lat, lng
        if self._has_fix():
            return self._person()
        if self.origin is not None:
            return self.origin
        raise DriveRouteError(
            'No simulated location yet. Pass from: {lat, lng}, or simulate a position first.',
        )

    def _has_fix(self) -> bool:
        if self.position is None:
            return False
        if getattr(self.position, 'true_lat', None) is not None:
            return True
        return self.position.current() is not None

    def _person(self) -> tuple[float, float]:
        if self.position is not None and getattr(self.position, 'true_lat', None) is not None:
            return (float(self.position.true_lat), float(self.position.true_lng))
        if self.position is not None:
            current = self.position.current()
            if current is not None:
                return current
        if self.origin is not None:
            return self.origin
        raise DriveRouteError('No simulated location yet.')

    def _car_point(self) -> tuple[float, float]:
        if self._car is not None:
            return self._car
        return self._person()

    def _resolve(self, item: Any) -> TripStop:
        if isinstance(item, str):
            item = {'address': item}
        if not isinstance(item, dict):
            raise DriveRouteError('Each stop must be an object')
        mode = str(item.get('mode') or 'drive').strip().lower()
        if mode not in ('drive', 'walk'):
            raise DriveRouteError('mode must be drive or walk')
        waypoint = _as_bool(item.get('waypoint'), False)
        if item.get('dwell_s') is not None and item.get('dwell_minutes') is None and item.get('dwell') is None:
            dwell_s = float(item.get('dwell_s') or 0)
        else:
            minutes = item.get('dwell_minutes', item.get('dwell', 0))
            dwell_s = float(minutes or 0) * 60.0
        if dwell_s < 0:
            raise DriveRouteError('Dwell minutes cannot be negative')
        if dwell_s > 24 * 3600:
            raise DriveRouteError('Dwell is limited to 24 hours')
        if waypoint:
            dwell_s = 0.0
        go_inside = _as_bool(item.get('go_inside', item.get('inside', True)), True)
        if mode == 'walk' or waypoint:
            go_inside = False
        label = item.get('label')
        address = item.get('address') or item.get('q') or item.get('query')
        if item.get('lat') is not None or item.get('lng') is not None:
            try:
                lat = float(item['lat'])
                lng = float(item['lng'])
            except (KeyError, TypeError, ValueError) as exc:
                raise DriveRouteError('lat and lng must be numbers') from exc
            Coordinate(latitude=lat, longitude=lng).validate()
            if not label:
                label = str(address) if address else f'{lat:.5f}, {lng:.5f}'
        else:
            if not address:
                raise DriveRouteError('Each stop needs lat/lng or an address')
            lat, lng, found = resolve_place({'address': address}, self._geocoder)
            label = label or found
        stop = TripStop(
            id=str(item['id'])[:32] if item.get('id') else _new_id(),
            lat=lat,
            lng=lng,
            label=str(label),
            mode=mode,
            dwell_s=dwell_s,
            go_inside=go_inside,
            waypoint=waypoint,
            address=str(address) if address else None,
        )
        return stop

    def _build_preview(self, origin: tuple[float, float]) -> None:
        person = origin
        car = origin
        for stop in self.stops:
            self._fill_preview(stop, person, car)
            person, car = self._pose_after(stop, person, car)

    def _fill_preview(
        self,
        stop: TripStop,
        person: tuple[float, float],
        car: tuple[float, float],
    ) -> None:
        parts: list[dict[str, Any]] = []
        travel = 0.0
        distance = 0.0
        walk_s = 0.0
        drive_s = 0.0
        walk_in_s = 0.0
        walk_back_s = 0.0
        signs = 0
        signals = 0
        expected = 0.0
        if stop.mode == 'drive':
            gap = haversine_m(person[0], person[1], car[0], car[1])
            if gap > CAR_GAP_M:
                back = self._preview_walk(person, car, short=gap <= SHORT_STRAIGHT_M)
                if back is not None:
                    parts.append(_part('walk', 'walk_to_car', back))
                    walk_back_s = back.duration_s
                    walk_s += back.duration_s
                    travel += back.duration_s
                    distance += back.distance_m
            plan, road = self._drive_router(car, (stop.lat, stop.lng), self.include_stops, self.offset)
            stop.road_lat, stop.road_lng = road
            summary = plan.stops_summary or {}
            signs = int(summary.get('stop_signs') or 0)
            signals = int(summary.get('signals') or 0)
            expected = float(summary.get('expected_signal_s') or 0)
            drive_s = float(plan.eta_s or plan.duration_s)
            parts.append({
                'mode': 'drive',
                'role': 'drive',
                'geometry': plan.geometry,
                'distance_m': round(plan.distance_m, 1),
                'duration_s': round(drive_s, 1),
                'provider': plan.provider,
                'stop_signs': signs,
                'signals': signals,
            })
            travel += drive_s
            distance += plan.distance_m
            gap = haversine_m(road[0], road[1], stop.lat, stop.lng)
            if stop.go_inside and not stop.waypoint and gap >= INSIDE_GAP_M:
                walked = build_walk_plan(
                    [road, (stop.lat, stop.lng)],
                    self.walk_speed,
                    self._rng,
                    self.walk_variation,
                    allow_pause=False,
                    provider='walk-inside',
                )
                if walked is not None:
                    parts.append(_part('walk', 'walk_in', walked))
                    walk_in_s = walked.duration_s
                    walk_s += walked.duration_s
                    travel += walked.duration_s
                    distance += walked.distance_m
        else:
            walked = self._preview_walk(person, (stop.lat, stop.lng), short=False)
            if walked is not None:
                parts.append(_part('walk', 'walk', walked))
                walk_s = walked.duration_s
                travel += walked.duration_s
                distance += walked.distance_m
            stop.road_lat = None
            stop.road_lng = None
        stop.parts = parts
        stop.travel_s = travel
        stop.distance_m = distance
        stop.drive_s = drive_s
        stop.walk_s = walk_s
        stop.walk_in_s = walk_in_s
        stop.walk_back_s = walk_back_s
        stop.stop_signs = signs
        stop.signals = signals
        stop.expected_signal_s = expected

    def _pose_after(
        self,
        stop: TripStop,
        person: tuple[float, float],
        car: tuple[float, float],
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        if stop.mode == 'drive':
            road = (stop.road_lat, stop.road_lng) if stop.road_lat is not None else car
            car = (float(road[0]), float(road[1]))
            if stop.go_inside and not stop.waypoint and stop.walk_in_s > 0:
                person = (stop.lat, stop.lng)
            else:
                person = car
            return person, car
        return (stop.lat, stop.lng), car

    def _preview_walk(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        short: bool,
    ) -> Optional[DrivePlan]:
        distance = haversine_m(start[0], start[1], end[0], end[1])
        if distance < 1.0:
            return None
        if short:
            points, provider = _straight_points(start, end), 'straight'
        else:
            points, provider = self._walk_router(start, end)
        return build_walk_plan(
            points,
            self.walk_speed,
            self._rng,
            self.walk_variation,
            allow_pause=not short and distance >= PAUSE_MIN_M,
            provider=provider,
        )

    def _queue_for(self, stop: TripStop, person: tuple[float, float], car: tuple[float, float]) -> list[str]:
        queue: list[str] = []
        if stop.mode == 'drive':
            if (not self._in_car) and haversine_m(person[0], person[1], car[0], car[1]) > CAR_GAP_M:
                queue.append('walk_to_car')
            queue.append('drive')
            if stop.go_inside and not stop.waypoint:
                queue.append('walk_in')
            if stop.dwell_s > 0 and not stop.waypoint:
                queue.append('dwell')
        else:
            if haversine_m(person[0], person[1], stop.lat, stop.lng) >= 1.5:
                queue.append('walk')
            if stop.dwell_s > 0 and not stop.waypoint:
                queue.append('dwell')
            if not queue:
                queue.append('walk')
        return queue

    def _begin_stop(self, index: int) -> None:
        if index >= len(self.stops):
            self._finish_trip()
            return
        self.index = index
        stop = self.stops[index]
        self._queue = self._queue_for(stop, self._person(), self._car_point())
        self._start_phase()

    def _begin_after_phase(self) -> None:
        """Caller holds ``_step_lock``."""
        self._start_phase()

    def _start_phase(self) -> None:
        if self.state != 'playing':
            return
        if not self._queue:
            stop = self.stops[self.index]
            stamp = _now_iso()
            if stop.arrived_at is None:
                stop.arrived_at = stamp
            stop.departed_at = stamp
            self._begin_stop(self.index + 1)
            return
        phase = self._queue[0]
        self.phase = phase
        stop = self.stops[self.index]
        if phase == 'dwell':
            self._start_dwell(stop)
            return
        if phase == 'walk_in':
            self.owns_drive = False
            self._in_car = False
            road = (stop.road_lat, stop.road_lng) if stop.road_lat is not None else self._car_point()
            gap = haversine_m(road[0], road[1], stop.lat, stop.lng)
            if gap < INSIDE_GAP_M:
                self._queue.pop(0)
                if stop.arrived_at is None:
                    stop.arrived_at = _now_iso()
                self._set_person(stop.lat, stop.lng)
                self._start_phase()
                return
            plan = build_walk_plan(
                [road, (stop.lat, stop.lng)],
                self.walk_speed,
                self._rng,
                self.walk_variation,
                allow_pause=False,
                provider='walk-inside',
            )
            self._play(plan, 'walk_in')
            return
        if phase == 'walk_to_car':
            self.owns_drive = False
            self._in_car = False
            car = self._car_point()
            person = self._person()
            distance = haversine_m(person[0], person[1], car[0], car[1])
            plan = self._movement_walk(person, car, short=distance <= SHORT_STRAIGHT_M)
            self._play(plan, 'walk_to_car')
            return
        if phase == 'walk':
            self.owns_drive = False
            self._in_car = False
            plan = self._movement_walk(self._person(), (stop.lat, stop.lng), short=False)
            if plan is None:
                self._queue.pop(0)
                self._set_person(stop.lat, stop.lng)
                if stop.arrived_at is None:
                    stop.arrived_at = _now_iso()
                self._start_phase()
                return
            self._play(plan, 'walk')
            return
        if phase == 'drive':
            self._play_drive(stop)
            return

    def _movement_walk(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        short: bool,
    ) -> Optional[DrivePlan]:
        distance = haversine_m(start[0], start[1], end[0], end[1])
        if distance < 1.0:
            return None
        if short:
            points, provider = _straight_points(start, end), 'straight'
        else:
            points, provider = self._walk_router(start, end)
        return build_walk_plan(
            points,
            self.walk_speed,
            self._rng,
            self.walk_variation,
            allow_pause=not short and distance >= PAUSE_MIN_M,
            provider=provider,
        )

    def _play(self, plan: Optional[DrivePlan], role: str) -> None:
        if plan is None:
            if self._queue and self._queue[0] == role:
                self._queue.pop(0)
            self._start_phase()
            return
        self._active_plan = plan
        self._remember_part(role, plan)
        self._token += 1
        self._awaiting = True
        self.owns_drive = False
        self.drive.play_plan(plan)

    def _play_drive(self, stop: TripStop) -> None:
        start = self._car_point() if self._in_car else self._person()
        if self._in_car:
            start = self._car_point()
        self.owns_drive = True
        self._in_car = True
        self._token += 1
        self._awaiting = True
        self._ignore_drive = True
        try:
            try:
                plan = self.drive.plan(
                    start,
                    (stop.lat, stop.lng),
                    offset_pct=self.offset,
                    provider=self.provider,
                    google_api_key=self.google_key,
                    include_stops=self.include_stops,
                    stops_seed=self._stops_seed,
                    anchor_end=False,
                )
            except DriveRouteError as exc:
                if 'same place' in str(exc).lower():
                    self._awaiting = False
                    self.owns_drive = False
                    stop.road_lat, stop.road_lng = stop.lat, stop.lng
                    self._car = (stop.lat, stop.lng)
                    if self._queue and self._queue[0] == 'drive':
                        self._queue.pop(0)
                    self._start_phase()
                    return
                raise
            self._active_plan = plan
            if plan.geometry:
                stop.road_lat = float(plan.geometry[-1][0])
                stop.road_lng = float(plan.geometry[-1][1])
            summary = plan.stops_summary or {}
            stop.stop_signs = int(summary.get('stop_signs') or 0)
            stop.signals = int(summary.get('signals') or 0)
            stop.expected_signal_s = float(summary.get('expected_signal_s') or 0)
            stop.drive_s = float(plan.eta_s or plan.duration_s)
            self._remember_part('drive', plan)
            self.drive.start()
        finally:
            self._ignore_drive = False

    def _start_dwell(self, stop: TripStop) -> None:
        self.owns_drive = False
        self._awaiting = False
        self._active_plan = None
        self._dwell_total = stop.dwell_s
        self._dwell_elapsed = 0.0
        self._dwell_mark = self._now()
        if stop.mode == 'walk' or stop.go_inside:
            point = (stop.lat, stop.lng)
        elif stop.road_lat is not None:
            point = (stop.road_lat, stop.road_lng)
        else:
            point = self._car_point()
        self._set_person(point[0], point[1])
        if stop.arrived_at is None:
            stop.arrived_at = _now_iso()
        if stop.mode == 'drive' and stop.go_inside:
            self._in_car = False
            self._enter_indoor()
        elif stop.mode == 'drive':
            self._in_car = True
        else:
            self._in_car = False

    def _complete_movement(self, token: int) -> None:
        try:
            with self._step_lock:
                if token != self._token or self.phase not in MOVEMENT or self.state != 'playing':
                    return
                self._token += 1
                phase = self.phase
                plan = self._active_plan
                self._awaiting = False
                self.owns_drive = False
                if plan is not None:
                    sample = position_at(plan, plan.duration_s)
                    self._apply_arrival(phase, sample.lat, sample.lng)
                if self._queue and self._queue[0] == phase:
                    self._queue.pop(0)
                if phase in ('walk', 'walk_in', 'drive') and self.index < len(self.stops):
                    stop = self.stops[self.index]
                    arriving = not self._queue or self._queue[0] == 'dwell'
                    if arriving and stop.arrived_at is None:
                        stop.arrived_at = _now_iso()
                self._start_phase()
        except Exception as exc:
            logger.warning(f'Trip could not continue: {exc}')
            self.state = 'paused'
            self._awaiting = False
            self.owns_drive = False
            self._error = str(exc)
        self._persist(force=True)
        self._emit()

    def _apply_arrival(self, phase: str, lat: float, lng: float) -> None:
        self._set_person(lat, lng)
        stop = self.stops[self.index] if self.index < len(self.stops) else None
        if phase == 'drive':
            self._car = (lat, lng)
            self._in_car = True
            if stop is not None:
                stop.road_lat, stop.road_lng = lat, lng
        elif phase == 'walk_to_car':
            self._in_car = True
            self._car = (lat, lng)
        else:
            self._in_car = False

    def _restart_phase(self) -> None:
        if self.phase == 'drive':
            person = self._person()
            self._car = person
            self._in_car = True
        if not self._queue or self._queue[0] != self.phase:
            self._queue.insert(0, self.phase)
        self._start_phase()

    def _restart_toward_current(self) -> None:
        self._token += 1
        self._awaiting = False
        self.owns_drive = False
        self._silence_drive_stop()
        self._leave_indoor()
        stop = self.stops[self.index]
        stop.arrived_at = None
        stop.departed_at = None
        if self.phase == 'drive':
            self._car = self._person()
            self._in_car = True
        self._queue = self._queue_for(stop, self._person(), self._car_point())
        self._refill_from_current()
        if self.state == 'playing':
            self._start_phase()

    def _refill_future(self) -> None:
        if self.index + 1 >= len(self.stops):
            return
        person, car = self._projected_pose()
        for stop in self.stops[self.index + 1:]:
            self._fill_preview(stop, person, car)
            person, car = self._pose_after(stop, person, car)

    def _refill_from_current(self) -> None:
        person = self._person()
        car = self._car_point()
        for stop in self.stops[self.index:]:
            self._fill_preview(stop, person, car)
            person, car = self._pose_after(stop, person, car)

    def _projected_pose(self) -> tuple[tuple[float, float], tuple[float, float]]:
        stop = self.stops[self.index]
        return self._pose_after(stop, self._person(), self._car_point())

    def _finish_trip(self) -> None:
        self.state = 'finished'
        self.phase = 'finished'
        self._queue = []
        self._awaiting = False
        self.owns_drive = False
        self._leave_indoor()
        if self.stops:
            last = self.stops[-1]
            stamp = _now_iso()
            if last.arrived_at is None:
                last.arrived_at = stamp
            if last.departed_at is None:
                last.departed_at = stamp

    def _cancel_from_drive(self) -> None:
        with self._step_lock:
            self._token += 1
            self._awaiting = False
            self.owns_drive = False
            self._leave_indoor()
            self.state = 'idle'
            self.phase = 'idle'
            self._queue = []
            self._error = None
        self._persist(force=True)
        self._emit()

    def _remember_part(self, role: str, plan: DrivePlan) -> None:
        if self.index >= len(self.stops):
            return
        stop = self.stops[self.index]
        geometry = [[round(lat, 6), round(lng, 6)] for lat, lng in plan.geometry]
        updated = False
        for part in stop.parts:
            if part.get('role') == role:
                part['geometry'] = geometry
                part['distance_m'] = round(plan.distance_m, 1)
                part['duration_s'] = round(plan.duration_s, 1)
                part['provider'] = plan.provider
                updated = True
                break
        if not updated:
            stop.parts.append({
                'mode': 'walk' if role != 'drive' else 'drive',
                'role': role,
                'geometry': geometry,
                'distance_m': round(plan.distance_m, 1),
                'duration_s': round(plan.duration_s, 1),
                'provider': plan.provider,
            })

    def _remaining_from(self, resolved: list[TripStop], done: list[TripStop]) -> list[TripStop]:
        done_ids = {stop.id for stop in done}
        if done and len(resolved) >= len(done) and all(
            resolved[index].id == done[index].id or _same_place(resolved[index], done[index])
            for index in range(len(done))
        ):
            return resolved[len(done):]
        stripped = [stop for stop in resolved if stop.id not in done_ids]
        if stripped and done_ids.intersection(stop.id for stop in resolved):
            return stripped
        if not done_ids.intersection(stop.id for stop in resolved) and len(resolved) > len(done):
            return resolved[len(done):]
        return stripped or resolved

    def _silence_drive_stop(self) -> None:
        self._ignore_drive = True
        try:
            self.drive.stop(user=False)
        except Exception as exc:
            logger.debug(f'Drive stop during trip failed: {exc}')
        finally:
            self._ignore_drive = False

    def _set_person(self, lat: float, lng: float) -> None:
        if self.position is None:
            self.origin = (lat, lng)
            return
        try:
            self.position.note(lat, lng, source='trip')
        except Exception as exc:
            logger.debug(f'Could not record trip position: {exc}')

    def _enter_indoor(self) -> None:
        if self.position is None:
            return
        try:
            current = float(self.position.snapshot().get('accuracy_m') or 0)
        except Exception:
            return
        if self._accuracy_saved is None:
            self._accuracy_saved = current
        if current < INDOOR_ACCURACY_M:
            self.position.set_accuracy(INDOOR_ACCURACY_M)

    def _leave_indoor(self) -> None:
        if self._accuracy_saved is None or self.position is None:
            self._accuracy_saved = None
            return
        saved = self._accuracy_saved
        self._accuracy_saved = None
        try:
            self.position.set_accuracy(saved)
        except Exception as exc:
            logger.debug(f'Could not restore GPS accuracy: {exc}')

    def _dwell_done(self) -> float:
        if self.phase != 'dwell':
            return self._dwell_elapsed
        extra = 0.0 if self.state != 'playing' else max(0.0, self._now() - self._dwell_mark)
        return self._dwell_elapsed + extra

    def _phase_remaining(self) -> float:
        if self.phase in MOVEMENT:
            try:
                return max(0.0, float(self.drive.status().get('remaining_s') or 0))
            except Exception:
                return 0.0
        if self.phase == 'dwell':
            return max(0.0, self._dwell_total - self._dwell_done())
        return 0.0

    def _estimate_phase(self, phase: str, stop: TripStop) -> float:
        if phase == 'dwell':
            return 0.0 if stop.waypoint else stop.dwell_s
        if phase == 'walk_in':
            return stop.walk_in_s
        if phase == 'walk_to_car':
            return stop.walk_back_s
        if phase == 'drive':
            return stop.drive_s
        if phase == 'walk':
            return stop.walk_s or stop.travel_s
        return 0.0

    def _remaining_seconds(
        self,
        stops: list[TripStop],
        state: str,
        phase: str,
        index: int,
        queue: list[str],
    ) -> float:
        if not stops or state == 'finished':
            return 0.0
        if state not in ('playing', 'paused'):
            return sum(stop.travel_s + (0.0 if stop.waypoint else stop.dwell_s) for stop in stops)
        if index >= len(stops):
            return 0.0
        total = self._phase_remaining() if state in ('playing', 'paused') else 0.0
        stop = stops[index]
        for upcoming in queue[1:]:
            total += self._estimate_phase(upcoming, stop)
        for later in stops[index + 1:]:
            total += later.travel_s + (0.0 if later.waypoint else later.dwell_s)
        return total

    def _clock_times(
        self,
        stops: list[TripStop],
        state: str,
        phase: str,
        index: int,
        queue: list[str],
    ) -> list[tuple[Optional[str], Optional[str]]]:
        if not stops:
            return []
        now = datetime.now(timezone.utc).replace(microsecond=0)
        rows: list[tuple[Optional[str], Optional[str]]] = []
        if state in ('playing', 'paused') and index < len(stops):
            for stop in stops[:index]:
                rows.append((stop.arrived_at, stop.departed_at))
            stop = stops[index]
            until_arrive = 0.0
            until_leave = self._phase_remaining()
            if phase == 'dwell':
                until_arrive = 0.0
            else:
                until_arrive = self._phase_remaining()
                for upcoming in queue[1:]:
                    if upcoming == 'dwell':
                        break
                    until_arrive += self._estimate_phase(upcoming, stop)
            for upcoming in queue[1:]:
                until_leave += self._estimate_phase(upcoming, stop)
            arrive = stop.arrived_at or (now + timedelta(seconds=until_arrive)).isoformat()
            leave = (now + timedelta(seconds=until_leave)).isoformat()
            rows.append((arrive, leave))
            cursor = now + timedelta(seconds=until_leave)
            for later in stops[index + 1:]:
                cursor = cursor + timedelta(seconds=later.travel_s)
                arrive_at = cursor
                cursor = cursor + timedelta(seconds=0.0 if later.waypoint else later.dwell_s)
                rows.append((arrive_at.isoformat(), cursor.isoformat()))
            return rows
        cursor = now
        for stop in stops:
            cursor = cursor + timedelta(seconds=stop.travel_s)
            arrive_at = cursor
            cursor = cursor + timedelta(seconds=0.0 if stop.waypoint else stop.dwell_s)
            rows.append((arrive_at.isoformat(), cursor.isoformat()))
        return rows

    def _detail(self, state: str, phase: str, label: Optional[str], dwell_left: float) -> str:
        name = label or 'the stop'
        if state == 'finished':
            return 'Trip complete'
        if state == 'idle':
            return 'Ready' if self.stops else 'No trip'
        prefix = 'Paused · ' if state == 'paused' else ''
        if phase == 'dwell':
            return f'{prefix}Dwelling at {name} — {_fmt_clock(dwell_left)} left'
        if phase == 'drive':
            return f'{prefix}Driving to {name}'
        if phase == 'walk':
            return f'{prefix}Walking to {name}'
        if phase == 'walk_in':
            return f'{prefix}Walking into {name}'
        if phase == 'walk_to_car':
            return f'{prefix}Walking back to the car'
        return prefix + 'Trip'

    def _emit(self) -> None:
        if self._emit_fn is None:
            return
        try:
            self._emit_fn('trip', self.status())
        except Exception as exc:
            logger.debug(f'Trip emit failed: {exc}')

    def _persist(self, force: bool = False) -> None:
        now = self._now()
        if not force and now - self._last_save < 2:
            return
        self._last_save = now
        payload = {
            'version': 1,
            'state': self.state,
            'phase': self.phase,
            'index': self.index,
            'queue': self._queue,
            'dwell_total': self._dwell_total,
            'dwell_elapsed': self._dwell_done() if self.phase == 'dwell' else self._dwell_elapsed,
            'in_car': self._in_car,
            'car': None if self._car is None else {'lat': self._car[0], 'lng': self._car[1]},
            'origin': None if self.origin is None else {'lat': self.origin[0], 'lng': self.origin[1]},
            'include_stops': self.include_stops,
            'speed_offset_pct': self.offset,
            'walk_speed_mps': self.walk_speed,
            'walk_variation': self.walk_variation,
            'provider': self.provider,
            'stops': [stop.to_dict() for stop in self.stops],
            'error': self._error,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(payload), encoding='utf-8')
            os.replace(temporary, self.path)
        except OSError as exc:
            logger.debug(f'Could not persist trip: {exc}')

    def _read_persisted(self) -> Optional[dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    def _restore(self, data: dict[str, Any]) -> None:
        self.stops = [TripStop.from_dict(item) for item in data.get('stops') or [] if isinstance(item, dict)]
        self.state = data.get('state') if data.get('state') in ('idle', 'playing', 'paused', 'finished') else 'idle'
        self.phase = str(data.get('phase') or 'idle')
        self.index = int(data.get('index') or 0)
        self._queue = [str(item) for item in data.get('queue') or []]
        self._dwell_total = float(data.get('dwell_total') or 0)
        self._dwell_elapsed = float(data.get('dwell_elapsed') or 0)
        self._dwell_mark = self._now()
        self._in_car = bool(data.get('in_car', True))
        car = data.get('car') or {}
        if isinstance(car, dict) and car.get('lat') is not None:
            self._car = (float(car['lat']), float(car['lng']))
        origin = data.get('origin') or {}
        if isinstance(origin, dict) and origin.get('lat') is not None:
            self.origin = (float(origin['lat']), float(origin['lng']))
        self.include_stops = bool(data.get('include_stops', True))
        self.offset = float(data.get('speed_offset_pct') or 0)
        self.walk_speed = float(data.get('walk_speed_mps') or WALK_MPS)
        self.walk_variation = float(data.get('walk_variation') or WALK_VARIATION)
        self.provider = str(data.get('provider') or 'osrm')
        self._error = data.get('error')
        if self.state == 'playing' and self.phase == 'drive':
            self._in_car = True

    def _read_library(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.library_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_library(self, payload: dict[str, Any]) -> None:
        self.library_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.library_path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(payload), encoding='utf-8')
        os.replace(temporary, self.library_path)


def _part(mode: str, role: str, plan: DrivePlan) -> dict[str, Any]:
    return {
        'mode': mode,
        'role': role,
        'geometry': [[round(lat, 6), round(lng, 6)] for lat, lng in plan.geometry],
        'distance_m': round(plan.distance_m, 1),
        'duration_s': round(plan.duration_s, 1),
        'provider': plan.provider,
    }


def _clean_name(name: str) -> str:
    text = (name or '').strip()
    if not text or len(text) > 80:
        raise DriveRouteError('A trip name is required')
    if any(char in text for char in '/\\'):
        raise DriveRouteError('A trip name cannot include a slash')
    return text


def register_trip_routes(app: Any, trip: TripService) -> None:
    """``/api/v1/trip`` for localhost scripts and ``/api/trip`` for the map UI."""

    def deny() -> Any:
        if request.path.startswith('/api/v1/') and not api_access_allowed(request.remote_addr):
            return jsonify({
                'ok': False,
                'error': 'The automation API listens on localhost only. Set GEOPORT_API_ALLOW_REMOTE=1 to override.',
            }), 403
        return None

    def body() -> dict[str, Any]:
        data = request.get_json(silent=True) or {}
        if isinstance(data, list):
            return {'stops': data}
        if not isinstance(data, dict):
            raise DriveRouteError('JSON object expected')
        return data

    def respond(fn: Callable[[], dict[str, Any]]) -> Any:
        blocked = deny()
        if blocked is not None:
            return blocked
        try:
            return jsonify(fn())
        except DriveRouteError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), exc.status
        except (TypeError, ValueError) as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.error(f'Trip request failed: {exc}')
            return jsonify({'ok': False, 'error': 'Trip request failed'}), 502

    @app.route('/api/v1/trip', methods=['GET', 'POST'])
    @app.route('/api/trip', methods=['GET', 'POST'])
    def trip_root() -> Any:
        if request.method == 'GET':
            return respond(trip.status)
        return respond(lambda: trip.plan_body(body()))

    @app.route('/api/v1/trip/start', methods=['POST'])
    @app.route('/api/trip/start', methods=['POST'])
    def trip_start() -> Any:
        return respond(trip.start)

    @app.route('/api/v1/trip/pause', methods=['POST'])
    @app.route('/api/trip/pause', methods=['POST'])
    def trip_pause() -> Any:
        return respond(trip.pause)

    @app.route('/api/v1/trip/resume', methods=['POST'])
    @app.route('/api/trip/resume', methods=['POST'])
    def trip_resume() -> Any:
        return respond(trip.resume)

    @app.route('/api/v1/trip/skip', methods=['POST'])
    @app.route('/api/trip/skip', methods=['POST'])
    def trip_skip() -> Any:
        return respond(trip.skip)

    @app.route('/api/v1/trip/stop', methods=['POST'])
    @app.route('/api/trip/stop', methods=['POST'])
    def trip_stop() -> Any:
        return respond(lambda: trip.stop(user=True))

    @app.route('/api/v1/trip/stops', methods=['POST', 'PATCH'])
    @app.route('/api/trip/stops', methods=['POST', 'PATCH'])
    def trip_stops() -> Any:
        def edit() -> dict[str, Any]:
            payload = body()
            stops = payload.get('stops', payload.get('remaining'))
            if stops is None:
                raise DriveRouteError('stops is required')
            return trip.replace_stops(stops)
        return respond(edit)

    @app.route('/api/v1/trip/library', methods=['GET', 'POST', 'DELETE'])
    @app.route('/api/trip/library', methods=['GET', 'POST', 'DELETE'])
    def trip_library() -> Any:
        if request.method == 'GET':
            return respond(trip.library)

        def change() -> dict[str, Any]:
            payload = body()
            name = str(payload.get('name') or '')
            if request.method == 'DELETE':
                return trip.delete_named(name)
            return trip.save_named(name)
        return respond(change)

    @app.route('/api/v1/trip/library/load', methods=['POST'])
    @app.route('/api/trip/library/load', methods=['POST'])
    def trip_library_load() -> Any:
        def load() -> dict[str, Any]:
            payload = body()
            return trip.load_named(str(payload.get('name') or ''), start=_as_bool(payload.get('start'), False))
        return respond(load)
