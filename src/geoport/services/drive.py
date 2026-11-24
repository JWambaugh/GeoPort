"""Driving-route planner and playback.

Plans a road route and moves the simulated location along it at each
segment's speed. The clock lives here so a test can replay a route
without a phone or a browser.

Speed for each segment, in order:

1. Posted limit from Valhalla's ``speed_limit`` (OpenStreetMap ``maxspeed``)
   when the public Valhalla trace matches the route.
2. OSRM car-profile speed annotation. That value is the speed the router
   used, which is the OSM limit when the road is tagged and a highway-class
   default when it is not. The public OSRM servers do not say which.
3. Valhalla's modeled edge speed, then a speed chosen from the road class,
   then 50 km/h.

OSRM (routing.openstreetmap.de, then router.project-osrm.org) is the
default router: free, no key, same family as the walking route the map
already draws. Google Directions is optional and only changes the roads
chosen. It does not provide speed limits; posted limits still come from
the Valhalla/OSM step when that match succeeds.

Playback ticks call the same device path as GPX: ``LocationSimulation.set``
via ``DeviceSessionManager.set_location``. Socket.IO only mirrors the tick
onto the map.
"""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

import requests
from flask import jsonify, request

from geoport.config import logger
from geoport.core.models import Coordinate

EARTH_RADIUS_M = 6_371_000.0
ACCEL_MPS2 = 2.4
BRAKE_MPS2 = 3.0
MIN_CORNER_MPS = 4.0
MIN_SPEED_KMH = 5.0
MAX_SPEED_KMH = 180.0
DEFAULT_SPEED_KMH = 50.0
DEFAULT_TICK_S = float(os.getenv('GEOPORT_DRIVE_TICK_S', '0.25'))
OSRM_PRIMARY = os.getenv(
    'GEOPORT_OSRM_URL',
    'https://routing.openstreetmap.de/routed-car/route/v1/driving/',
)
OSRM_FALLBACK = os.getenv(
    'GEOPORT_OSRM_FALLBACK_URL',
    'https://router.project-osrm.org/route/v1/driving/',
)
VALHALLA_TRACE_URL = os.getenv(
    'GEOPORT_VALHALLA_TRACE_URL',
    'https://valhalla1.openstreetmap.de/trace_attributes',
)
GOOGLE_DIRECTIONS_URL = 'https://maps.googleapis.com/maps/api/directions/json'
USER_AGENT = 'GeoPort/5.0 (self-hosted personal iOS location simulator)'
_HTTP_TIMEOUT = (3.05, 12)
_VALHALLA_TIMEOUT = (3.05, 8)

# Used only when neither a posted limit nor a router speed is known.
ROAD_CLASS_FALLBACK_KMH = {
    'motorway': 100.0,
    'trunk': 80.0,
    'primary': 60.0,
    'secondary': 50.0,
    'tertiary': 50.0,
    'unclassified': 50.0,
    'residential': 30.0,
    'living_street': 15.0,
    'service': 20.0,
    'service_other': 30.0,
    'track': 20.0,
}
SOURCE_LABELS = {
    'osm_maxspeed': 'posted limit',
    'osrm_annotation': 'OSRM estimate',
    'valhalla_speed': 'modeled speed',
    'road_class': 'road class',
    'google_pace': 'Google pace',
    'fallback': 'default 50 km/h',
    'approach': 'off the road',
    'simulate': 'chosen speed',
}
APPROACH_MAX_KMH = 30.0


class DriveRouteError(Exception):
    """A drive could not be planned or controlled. ``status`` is the HTTP code."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class MicroEdge:
    """One piece of road geometry and the limit used to drive it."""

    lat1: float
    lng1: float
    lat2: float
    lng2: float
    length_m: float
    limit_kmh: Optional[float]
    source: str
    road_class: Optional[str] = None
    name: Optional[str] = None
    d0: float = 0.0
    d1: float = 0.0


@dataclass
class ValEdge:
    length_m: float
    limit_kmh: Optional[float]
    speed_kmh: Optional[float]
    road_class: Optional[str]
    name: Optional[str]


@dataclass
class Phase:
    """Constant-acceleration piece of the speed profile, along route distance."""

    t0: float
    t1: float
    d0: float
    d1: float
    v0: float
    v1: float
    segment_index: int


@dataclass
class DriveSample:
    t: float
    lat: float
    lng: float
    speed_mps: float
    distance_m: float
    segment_index: int
    finished: bool
    duration_s: float
    limit_kmh: float
    source: str
    road_class: Optional[str]
    name: Optional[str]
    speed_offset_pct: float

    @property
    def speed_kmh(self) -> float:
        return self.speed_mps * 3.6

    def to_dict(self) -> dict[str, Any]:
        duration = self.duration_s
        progress = 0.0 if duration <= 0 else min(1.0, self.t / duration)
        return {
            't': round(self.t, 3),
            'lat': round(self.lat, 7),
            'lng': round(self.lng, 7),
            'speed_kmh': round(self.speed_kmh, 2),
            'limit_kmh': round(self.limit_kmh, 1),
            'distance_m': round(self.distance_m, 2),
            'segment': self.segment_index,
            'finished': self.finished,
            'duration_s': round(duration, 2),
            'remaining_s': round(max(0.0, duration - self.t), 2),
            'progress': round(progress, 5),
            'source': self.source,
            'road_class': self.road_class,
            'name': self.name,
            'speed_offset_pct': self.speed_offset_pct,
        }


@dataclass
class DrivePlan:
    segments: list[MicroEdge]
    phases: list[Phase]
    distance_m: float
    duration_s: float
    provider: str
    speed_offset_pct: float
    geometry: list[list[float]]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            'ok': True,
            'provider': self.provider,
            'speed_offset_pct': self.speed_offset_pct,
            'distance_m': round(self.distance_m, 1),
            'duration_s': round(self.duration_s, 1),
            'speed_sources': _source_distances(self.segments),
            'speed_summary': sources_label(self.segments),
            'geometry': [[round(lat, 6), round(lng, 6)] for lat, lng in self.geometry],
            'segments': [
                {
                    'length_m': round(seg.length_m, 1),
                    'limit_kmh': round(seg.limit_kmh or 0.0, 1),
                    'source': seg.source,
                    'road_class': seg.road_class,
                    'name': seg.name,
                }
                for seg in self.segments
            ],
        }


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def turn_angle_deg(bearing_in: float, bearing_out: float) -> float:
    return abs((bearing_out - bearing_in + 180.0) % 360.0 - 180.0)


def clamp_offset(offset_pct: float) -> float:
    return max(-80.0, min(150.0, float(offset_pct)))


def effective_limit_kmh(limit_kmh: float, offset_pct: float) -> float:
    scaled = float(limit_kmh) * (1.0 + clamp_offset(offset_pct) / 100.0)
    return min(MAX_SPEED_KMH, max(MIN_SPEED_KMH, scaled))


def corner_speed_mps(v_in: float, v_out: float, angle_deg: float) -> float:
    """Speed to carry through a junction. Straight roads keep the slower cruise."""
    slower = min(max(v_in, 0.0), max(v_out, 0.0))
    if angle_deg < 12.0:
        return slower
    factor = 1.0 - (angle_deg - 12.0) / 160.0
    factor = max(0.22, factor)
    return max(MIN_CORNER_MPS, slower * factor)


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decode a Google/OSRM polyline into ``(lat, lng)`` points."""
    if not encoded:
        return []
    inv = 10 ** precision
    lat = 0
    lng = 0
    index = 0
    coords: list[tuple[float, float]] = []
    length = len(encoded)
    while index < length:
        delta_lat = _decode_polyline_delta(encoded, index)
        lat += delta_lat[0]
        index = delta_lat[1]
        delta_lng = _decode_polyline_delta(encoded, index)
        lng += delta_lng[0]
        index = delta_lng[1]
        coords.append((lat / inv, lng / inv))
    return coords


def _decode_polyline_delta(encoded: str, index: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while index < len(encoded):
        byte = ord(encoded[index]) - 63
        index += 1
        result |= (byte & 0x1F) << shift
        shift += 5
        if byte < 0x20:
            break
    delta = ~(result >> 1) if result & 1 else (result >> 1)
    return delta, index


def sources_label(segments: list[MicroEdge]) -> str:
    total = sum(seg.length_m for seg in segments) or 1.0
    buckets: dict[str, float] = {}
    for seg in segments:
        buckets[seg.source] = buckets.get(seg.source, 0.0) + seg.length_m
    parts = []
    for key, dist in sorted(buckets.items(), key=lambda item: item[1], reverse=True):
        pct = int(round(100.0 * dist / total))
        if pct <= 0:
            continue
        parts.append(f'{pct}% {SOURCE_LABELS.get(key, key)}')
    return ', '.join(parts)


def _source_distances(segments: list[MicroEdge]) -> dict[str, float]:
    buckets: dict[str, float] = {}
    for seg in segments:
        buckets[seg.source] = round(buckets.get(seg.source, 0.0) + seg.length_m, 1)
    return buckets


def segments_from_points(
    points: list[Any],
    speeds_kmh: list[Any],
    sources: Optional[list[Any]] = None,
    road_classes: Optional[list[Any]] = None,
    names: Optional[list[Any]] = None,
) -> list[MicroEdge]:
    """Build segments from ``[[lat, lng], ...]`` and one speed per span."""
    if len(points) < 2:
        raise DriveRouteError('Need at least two points')
    if len(speeds_kmh) != len(points) - 1:
        raise DriveRouteError('speeds_kmh must have one value per segment')
    segments: list[MicroEdge] = []
    for index in range(len(points) - 1):
        lat1, lng1 = _point(points[index])
        lat2, lng2 = _point(points[index + 1])
        length = haversine_m(lat1, lng1, lat2, lng2)
        if length < 0.4:
            continue
        limit = float(speeds_kmh[index])
        source = 'simulate'
        if sources and index < len(sources) and sources[index]:
            source = str(sources[index])
        road_class = None
        if road_classes and index < len(road_classes) and road_classes[index]:
            road_class = str(road_classes[index])
        name = None
        if names and index < len(names) and names[index]:
            name = str(names[index])
        segments.append(MicroEdge(
            lat1=lat1,
            lng1=lng1,
            lat2=lat2,
            lng2=lng2,
            length_m=length,
            limit_kmh=limit if limit >= MIN_SPEED_KMH else None,
            source=source if limit >= MIN_SPEED_KMH else 'unset',
            road_class=road_class,
            name=name,
        ))
    if not segments:
        raise DriveRouteError('Route length is zero')
    finalize_limits(segments)
    return segments


def _point(value: Any) -> tuple[float, float]:
    if isinstance(value, dict):
        lat = float(value['lat'])
        lng = float(value.get('lng', value.get('lon')))
    else:
        lat = float(value[0])
        lng = float(value[1])
    Coordinate(latitude=lat, longitude=lng).validate()
    return lat, lng


def parse_osrm_route(payload: dict[str, Any]) -> list[MicroEdge]:
    code = payload.get('code')
    if code != 'Ok':
        message = payload.get('message') or code or 'No driving route'
        status = 404 if code == 'NoRoute' else 502
        raise DriveRouteError(f'OSRM: {message}', status=status)
    routes = payload.get('routes') or []
    if not routes:
        raise DriveRouteError('OSRM returned no route', status=404)
    geometry = (routes[0].get('geometry') or {}).get('coordinates') or []
    legs = routes[0].get('legs') or []
    annotation = (legs[0].get('annotation') if legs else None) or {}
    speeds = annotation.get('speed') or []
    distances = annotation.get('distance') or []
    if len(geometry) < 2:
        raise DriveRouteError('OSRM route geometry is empty', status=502)
    segments: list[MicroEdge] = []
    for index in range(len(geometry) - 1):
        lng1, lat1 = geometry[index]
        lng2, lat2 = geometry[index + 1]
        if index < len(distances):
            length = float(distances[index])
        else:
            length = haversine_m(lat1, lng1, lat2, lng2)
        if length < 0.4:
            continue
        limit = None
        source = 'unset'
        if index < len(speeds) and speeds[index] and float(speeds[index]) * 3.6 >= MIN_SPEED_KMH:
            limit = float(speeds[index]) * 3.6
            source = 'osrm_annotation'
        segments.append(MicroEdge(
            lat1=float(lat1),
            lng1=float(lng1),
            lat2=float(lat2),
            lng2=float(lng2),
            length_m=length,
            limit_kmh=limit,
            source=source,
        ))
    if not segments:
        raise DriveRouteError('OSRM route has no drivable length', status=404)
    return segments


def parse_valhalla_trace(payload: dict[str, Any]) -> list[ValEdge]:
    if not isinstance(payload, dict) or payload.get('error') or payload.get('error_code'):
        return []
    units = str(payload.get('units') or 'kilometers').lower()
    to_m = 1609.344 if units.startswith('mile') else 1000.0
    to_kmh = 1.60934 if units.startswith('mile') else 1.0
    edges: list[ValEdge] = []
    for edge in payload.get('edges') or []:
        length_m = float(edge.get('length') or 0) * to_m
        if length_m <= 0:
            continue
        edges.append(ValEdge(
            length_m=length_m,
            limit_kmh=_positive_speed(edge.get('speed_limit'), to_kmh),
            speed_kmh=_positive_speed(edge.get('speed'), to_kmh),
            road_class=edge.get('road_class') or None,
            name=_first_name(edge.get('names')),
        ))
    return edges


def _positive_speed(value: Any, to_kmh: float) -> Optional[float]:
    if isinstance(value, (int, float)) and float(value) > 0:
        speed = float(value) * to_kmh
        if speed >= MIN_SPEED_KMH:
            return speed
    return None


def _first_name(names: Any) -> Optional[str]:
    if isinstance(names, list) and names:
        text = str(names[0]).strip()
        return text or None
    return None


def apply_valhalla(segments: list[MicroEdge], val_edges: list[ValEdge]) -> bool:
    """Copy posted limits onto OSRM/Google segments when the traces agree.

    Returns False when the matched length is too different to trust.
    """
    route_m = sum(seg.length_m for seg in segments)
    trace_m = sum(edge.length_m for edge in val_edges)
    if route_m <= 0 or trace_m <= 0:
        return False
    ratio = trace_m / route_m
    if ratio < 0.75 or ratio > 1.25:
        logger.info(
            f'Valhalla trace length {trace_m:.0f}m vs route {route_m:.0f}m; keeping router speeds'
        )
        return False
    intervals: list[tuple[float, float, ValEdge]] = []
    cursor = 0.0
    for edge in val_edges:
        intervals.append((cursor, cursor + edge.length_m, edge))
        cursor += edge.length_m
    scale = trace_m / route_m
    cursor = 0.0
    for seg in segments:
        midpoint = (cursor + seg.length_m / 2.0) * scale
        cursor += seg.length_m
        edge = _interval_at(intervals, midpoint)
        if edge is None:
            continue
        if edge.road_class:
            seg.road_class = edge.road_class
        if edge.name and not seg.name:
            seg.name = edge.name
        if edge.limit_kmh:
            seg.limit_kmh = edge.limit_kmh
            seg.source = 'osm_maxspeed'
        elif not seg.limit_kmh:
            if edge.speed_kmh:
                seg.limit_kmh = edge.speed_kmh
                seg.source = 'valhalla_speed'
            elif edge.road_class:
                seg.limit_kmh = ROAD_CLASS_FALLBACK_KMH.get(edge.road_class, DEFAULT_SPEED_KMH)
                seg.source = 'road_class'
    return True


def _interval_at(intervals: list[tuple[float, float, ValEdge]], dist: float) -> Optional[ValEdge]:
    for index, (start, end, edge) in enumerate(intervals):
        if dist < end or index == len(intervals) - 1:
            return edge
    return None


def finalize_limits(segments: list[MicroEdge]) -> None:
    for seg in segments:
        if seg.limit_kmh and seg.limit_kmh >= MIN_SPEED_KMH:
            continue
        road_class = (seg.road_class or '').lower()
        if road_class in ROAD_CLASS_FALLBACK_KMH:
            seg.limit_kmh = ROAD_CLASS_FALLBACK_KMH[road_class]
            seg.source = 'road_class'
        else:
            seg.limit_kmh = DEFAULT_SPEED_KMH
            seg.source = 'fallback'


def parse_google_directions(payload: dict[str, Any]) -> list[MicroEdge]:
    status = payload.get('status')
    if status != 'OK':
        message = payload.get('error_message') or status or 'Google directions failed'
        code = 400 if status in {'REQUEST_DENIED', 'INVALID_REQUEST'} else 502
        raise DriveRouteError(f'Google: {message}', status=code)
    routes = payload.get('routes') or []
    if not routes:
        raise DriveRouteError('Google returned no route', status=404)
    legs = routes[0].get('legs') or []
    if not legs:
        raise DriveRouteError('Google route has no legs', status=502)
    segments: list[MicroEdge] = []
    previous: Optional[tuple[float, float]] = None
    for step in legs[0].get('steps') or []:
        encoded = ((step.get('polyline') or {}).get('points')) or ''
        coords = decode_polyline(encoded)
        if len(coords) < 2:
            continue
        distance_m = float((step.get('distance') or {}).get('value') or 0)
        duration_s = float((step.get('duration') or {}).get('value') or 0)
        pace = (distance_m / duration_s) * 3.6 if distance_m > 0 and duration_s > 0 else None
        name = _strip_html(step.get('html_instructions') or '') or None
        if previous and _same_point(previous, coords[0]):
            coords = coords[1:]
        for lat, lng in coords:
            if previous is None:
                previous = (lat, lng)
                continue
            length = haversine_m(previous[0], previous[1], lat, lng)
            if length >= 0.4:
                limit = pace if pace and pace >= MIN_SPEED_KMH else None
                segments.append(MicroEdge(
                    lat1=previous[0],
                    lng1=previous[1],
                    lat2=lat,
                    lng2=lng,
                    length_m=length,
                    limit_kmh=limit,
                    source='google_pace' if limit else 'unset',
                    name=name,
                ))
            previous = (lat, lng)
    if not segments:
        raise DriveRouteError('Google route has no drivable length', status=404)
    return segments


def _strip_html(value: str) -> str:
    text = []
    skipping = False
    for char in value:
        if char == '<':
            skipping = True
            continue
        if char == '>':
            skipping = False
            text.append(' ')
            continue
        if not skipping:
            text.append(char)
    return ' '.join(''.join(text).split())


def _same_point(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6


def downsample_coords(coords: list[tuple[float, float]], max_points: int) -> list[tuple[float, float]]:
    if len(coords) <= max_points or max_points < 2:
        return list(coords)
    last = len(coords) - 1
    indexes = {0, last}
    for step in range(max_points):
        indexes.add(min(last, int(round(step * last / (max_points - 1)))))
    return [coords[index] for index in sorted(indexes)]


def _segment_coords(segments: list[MicroEdge]) -> list[tuple[float, float]]:
    coords = [(segments[0].lat1, segments[0].lng1)]
    for seg in segments:
        coords.append((seg.lat2, seg.lng2))
    return coords


def fetch_osrm_segments(
    start: tuple[float, float],
    end: tuple[float, float],
    http: Any,
) -> list[MicroEdge]:
    errors: list[str] = []
    for base in (OSRM_PRIMARY, OSRM_FALLBACK):
        if not base:
            continue
        url = f'{base.rstrip("/")}/{start[1]},{start[0]};{end[1]},{end[0]}'
        try:
            response = http.get(
                url,
                params={
                    'overview': 'full',
                    'geometries': 'geojson',
                    'steps': 'false',
                    'annotations': 'speed,duration,distance',
                },
                timeout=_HTTP_TIMEOUT,
                headers={'User-Agent': USER_AGENT},
            )
            response.raise_for_status()
            return parse_osrm_route(response.json())
        except DriveRouteError:
            raise
        except Exception as exc:
            errors.append(str(exc))
            logger.info(f'OSRM driving request failed ({base}): {exc}')
    raise DriveRouteError('Driving route lookup failed: ' + '; '.join(errors), status=502)


def fetch_google_segments(
    start: tuple[float, float],
    end: tuple[float, float],
    api_key: str,
    http: Any,
) -> list[MicroEdge]:
    if not api_key.strip():
        raise DriveRouteError(
            'Google routing needs an API key. Save one in the drive settings, or use OSRM.',
        )
    try:
        response = http.get(
            GOOGLE_DIRECTIONS_URL,
            params={
                'origin': f'{start[0]},{start[1]}',
                'destination': f'{end[0]},{end[1]}',
                'mode': 'driving',
                'key': api_key.strip(),
            },
            timeout=_HTTP_TIMEOUT,
            headers={'User-Agent': USER_AGENT},
        )
        response.raise_for_status()
        payload = response.json()
    except DriveRouteError:
        raise
    except Exception as exc:
        logger.info(f'Google directions request failed ({type(exc).__name__})')
        raise DriveRouteError('Google directions request failed', status=502) from None
    return parse_google_directions(payload)


def fetch_valhalla_edges(segments: list[MicroEdge], http: Any) -> list[ValEdge]:
    coords = downsample_coords(_segment_coords(segments), 300)
    body = {
        'shape': [{'lat': lat, 'lon': lng} for lat, lng in coords],
        'costing': 'auto',
        'shape_match': 'map_snap',
        'units': 'kilometers',
        'filters': {
            'action': 'include',
            'attributes': [
                'edge.speed_limit',
                'edge.road_class',
                'edge.length',
                'edge.speed',
                'edge.names',
            ],
        },
    }
    try:
        response = http.post(
            VALHALLA_TRACE_URL,
            json=body,
            timeout=_VALHALLA_TIMEOUT,
            headers={'User-Agent': USER_AGENT, 'Content-Type': 'application/json'},
        )
        response.raise_for_status()
        return parse_valhalla_trace(response.json())
    except Exception as exc:
        logger.info(f'Valhalla speed-limit enrichment skipped: {exc}')
        return []


def fetch_segments(
    start: tuple[float, float],
    end: tuple[float, float],
    provider: str,
    google_api_key: str,
    http: Any,
) -> tuple[list[MicroEdge], str]:
    chosen = (provider or 'osrm').strip().lower()
    if chosen == 'google':
        segments = fetch_google_segments(start, end, google_api_key, http)
        used = 'google'
    elif chosen == 'osrm':
        segments = fetch_osrm_segments(start, end, http)
        used = 'osrm'
    else:
        raise DriveRouteError('Unknown routing provider. Use osrm or google.')
    val_edges = fetch_valhalla_edges(segments, http)
    if val_edges:
        apply_valhalla(segments, val_edges)
    finalize_limits(segments)
    anchor_endpoints(segments, start, end)
    return segments, used


def anchor_endpoints(
    segments: list[MicroEdge],
    start: tuple[float, float],
    end: tuple[float, float],
) -> None:
    """Join the clicked pins to the routed roads.

    OSRM and Google snap to the driving network, so a pin in a park or a
    building can sit off the first and last road vertices. A short approach
    keeps the drawn route and the moving pin on the points the user chose.
    """
    if not segments:
        return
    first = segments[0]
    gap_start = haversine_m(start[0], start[1], first.lat1, first.lng1)
    if gap_start >= 8.0:
        segments.insert(0, MicroEdge(
            lat1=start[0],
            lng1=start[1],
            lat2=first.lat1,
            lng2=first.lng1,
            length_m=gap_start,
            limit_kmh=min(first.limit_kmh or APPROACH_MAX_KMH, APPROACH_MAX_KMH),
            source='approach',
        ))
    last = segments[-1]
    gap_end = haversine_m(last.lat2, last.lng2, end[0], end[1])
    if gap_end >= 8.0:
        segments.append(MicroEdge(
            lat1=last.lat2,
            lng1=last.lng2,
            lat2=end[0],
            lng2=end[1],
            length_m=gap_end,
            limit_kmh=min(last.limit_kmh or APPROACH_MAX_KMH, APPROACH_MAX_KMH),
            source='approach',
        ))


def _distance_for_speed_change(v0: float, v1: float) -> float:
    if abs(v1 - v0) < 1e-6:
        return 0.0
    accel = ACCEL_MPS2 if v1 > v0 else BRAKE_MPS2
    return abs(v1 * v1 - v0 * v0) / (2.0 * accel)


def _speed_after(v0: float, distance_m: float, speeding_up: bool) -> float:
    accel = ACCEL_MPS2 if speeding_up else -BRAKE_MPS2
    return math.sqrt(max(0.0, v0 * v0 + 2.0 * accel * distance_m))


def _normal_knots(v_entry: float, v_cruise: float, v_exit: float, length_m: float) -> tuple[list[tuple[float, float]], float]:
    """Speed knots for a segment we are already at or below cruise."""
    target = max(v_cruise, 1.2)
    v_entry = min(max(v_entry, 0.0), target)
    v_exit = min(max(v_exit, 0.0), target)
    d_acc = _distance_for_speed_change(v_entry, target)
    d_brake = _distance_for_speed_change(target, v_exit)
    if d_acc + d_brake <= length_m + 1e-6:
        knots = [(0.0, v_entry)]
        if d_acc > 1e-3:
            knots.append((d_acc, target))
        cruise_end = length_m - d_brake
        if d_brake > 1e-3 and cruise_end - knots[-1][0] > 1e-3:
            knots.append((cruise_end, target))
        knots.append((length_m, v_exit))
        return knots, v_exit
    coef = 1.0 / (2.0 * ACCEL_MPS2) + 1.0 / (2.0 * BRAKE_MPS2)
    peak_sq = (length_m + (v_entry * v_entry) / (2.0 * ACCEL_MPS2) + (v_exit * v_exit) / (2.0 * BRAKE_MPS2)) / coef
    peak = math.sqrt(max(0.0, peak_sq))
    if peak + 1e-3 >= max(v_entry, v_exit):
        d_up = _distance_for_speed_change(v_entry, peak)
        d_down = _distance_for_speed_change(peak, v_exit)
        total = d_up + d_down
        if total > length_m and total > 0:
            d_up *= length_m / total
        knots = [(0.0, v_entry)]
        if d_up > 1e-3:
            knots.append((d_up, peak))
        knots.append((length_m, v_exit))
        return knots, v_exit
    speeding_up = v_exit >= v_entry
    reached = _speed_after(v_entry, length_m, speeding_up)
    if speeding_up:
        reached = min(reached, v_exit)
    else:
        reached = max(reached, v_exit)
    return [(0.0, v_entry), (length_m, reached)], reached


def _segment_knots(
    v_entry: float,
    v_cruise: float,
    v_exit: float,
    length_m: float,
) -> tuple[list[tuple[float, float]], float]:
    target = max(v_cruise, 1.2)
    v_entry = max(v_entry, 0.0)
    if v_entry <= target + 0.2:
        return _normal_knots(v_entry, target, v_exit, length_m)
    d_brake_in = _distance_for_speed_change(v_entry, target)
    if d_brake_in >= length_m:
        reached = _speed_after(v_entry, length_m, False)
        v_exit = min(max(v_exit, 0.0), max(target, v_entry))
        if v_exit < reached and _distance_for_speed_change(v_entry, v_exit) <= length_m:
            d_need = _distance_for_speed_change(v_entry, v_exit)
            return [(0.0, v_entry), (d_need, v_exit), (length_m, v_exit)], v_exit
        return [(0.0, v_entry), (length_m, reached)], reached
    rest_knots, v_out = _normal_knots(target, target, v_exit, length_m - d_brake_in)
    knots = [(0.0, v_entry), (d_brake_in, target)]
    for distance, speed in rest_knots[1:]:
        knots.append((distance + d_brake_in, speed))
    return knots, v_out


def _dedupe_knots(knots: list[tuple[float, float]], length_m: float) -> list[tuple[float, float]]:
    if not knots:
        return [(0.0, 0.0), (length_m, 0.0)]
    cleaned = [knots[0]]
    for distance, speed in knots[1:]:
        if distance < cleaned[-1][0] - 1e-6:
            continue
        if distance - cleaned[-1][0] < 1e-3:
            cleaned[-1] = (cleaned[-1][0], speed)
            continue
        cleaned.append((distance, speed))
    if cleaned[-1][0] < length_m - 1e-3:
        cleaned.append((length_m, cleaned[-1][1]))
    else:
        cleaned[-1] = (length_m, cleaned[-1][1])
    return cleaned


def _phases_from_knots(
    knots: list[tuple[float, float]],
    d_origin: float,
    t_origin: float,
    segment_index: int,
) -> tuple[list[Phase], float]:
    phases: list[Phase] = []
    clock = t_origin
    for (d0, v0), (d1, v1) in zip(knots, knots[1:]):
        distance = d1 - d0
        if distance <= 1e-4:
            continue
        if abs(v1 - v0) < 1e-4:
            dt = distance / max(v0, 0.3)
        else:
            accel = (v1 * v1 - v0 * v0) / (2.0 * distance)
            if abs(accel) < 1e-6:
                dt = distance / max(v0, 0.3)
            else:
                dt = (v1 - v0) / accel
        dt = abs(dt)
        phases.append(Phase(
            t0=clock,
            t1=clock + dt,
            d0=d_origin + d0,
            d1=d_origin + d1,
            v0=v0,
            v1=v1,
            segment_index=segment_index,
        ))
        clock += dt
    return phases, clock - t_origin


def build_profile(
    segments: list[MicroEdge],
    offset_pct: float = 0.0,
    provider: str = 'osrm',
) -> DrivePlan:
    if not segments:
        raise DriveRouteError('Route has no segments')
    offset = clamp_offset(offset_pct)
    copied: list[MicroEdge] = []
    distance = 0.0
    for seg in segments:
        item = replace(seg)
        item.d0 = distance
        item.d1 = distance + seg.length_m
        distance = item.d1
        copied.append(item)
    cruises = [effective_limit_kmh(seg.limit_kmh or DEFAULT_SPEED_KMH, offset) / 3.6 for seg in copied]
    bearings = [
        bearing_deg(seg.lat1, seg.lng1, seg.lat2, seg.lng2)
        for seg in copied
    ]
    phases: list[Phase] = []
    v_entry = 0.0
    route_distance = 0.0
    clock = 0.0
    for index, seg in enumerate(copied):
        if index == len(copied) - 1:
            v_exit = 0.0
        else:
            angle = turn_angle_deg(bearings[index], bearings[index + 1])
            v_exit = corner_speed_mps(cruises[index], cruises[index + 1], angle)
        knots, v_entry = _segment_knots(v_entry, cruises[index], v_exit, seg.length_m)
        knots = _dedupe_knots(knots, seg.length_m)
        seg_phases, _elapsed = _phases_from_knots(knots, route_distance, clock, index)
        phases.extend(seg_phases)
        if seg_phases:
            clock = seg_phases[-1].t1
        route_distance += seg.length_m
    if not phases:
        raise DriveRouteError('Could not build a speed profile for this route')
    geometry = [[copied[0].lat1, copied[0].lng1]]
    for seg in copied:
        geometry.append([seg.lat2, seg.lng2])
    return DrivePlan(
        segments=copied,
        phases=phases,
        distance_m=route_distance,
        duration_s=phases[-1].t1,
        provider=provider,
        speed_offset_pct=offset,
        geometry=geometry,
    )


def _phase_at(phases: list[Phase], t: float) -> Phase:
    lo = 0
    hi = len(phases) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if phases[mid].t1 < t:
            lo = mid + 1
        else:
            hi = mid
    return phases[lo]


def _point_at_distance(segments: list[MicroEdge], dist: float) -> tuple[float, float, int]:
    if dist <= segments[0].d0:
        return segments[0].lat1, segments[0].lng1, 0
    if dist >= segments[-1].d1:
        return segments[-1].lat2, segments[-1].lng2, len(segments) - 1
    lo = 0
    hi = len(segments) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if segments[mid].d1 < dist:
            lo = mid + 1
        else:
            hi = mid
    seg = segments[lo]
    fraction = 0.0 if seg.length_m <= 0 else (dist - seg.d0) / seg.length_m
    fraction = min(1.0, max(0.0, fraction))
    lat = seg.lat1 + (seg.lat2 - seg.lat1) * fraction
    lng = seg.lng1 + (seg.lng2 - seg.lng1) * fraction
    return lat, lng, lo


def position_at(plan: DrivePlan, t: float) -> DriveSample:
    """Location and speed at ``t`` seconds from the start, interpolated along the road."""
    finished = t >= plan.duration_s - 1e-6
    if not plan.phases:
        seg = plan.segments[0]
        return DriveSample(
            t=0.0, lat=seg.lat1, lng=seg.lng1, speed_mps=0.0, distance_m=0.0,
            segment_index=0, finished=True, duration_s=plan.duration_s,
            limit_kmh=seg.limit_kmh or DEFAULT_SPEED_KMH, source=seg.source,
            road_class=seg.road_class, name=seg.name, speed_offset_pct=plan.speed_offset_pct,
        )
    if t <= 0:
        phase = plan.phases[0]
        dist = phase.d0
        speed = phase.v0
        when = 0.0
    elif finished:
        phase = plan.phases[-1]
        dist = plan.distance_m
        speed = 0.0
        when = plan.duration_s
    else:
        phase = _phase_at(plan.phases, t)
        tau = min(max(0.0, t - phase.t0), max(0.0, phase.t1 - phase.t0))
        span = phase.t1 - phase.t0
        if span <= 1e-6:
            dist = phase.d0
            speed = phase.v0
        else:
            accel = (phase.v1 - phase.v0) / span
            speed = phase.v0 + accel * tau
            dist = phase.d0 + phase.v0 * tau + 0.5 * accel * tau * tau
            dist = min(phase.d1, max(phase.d0, dist))
        when = t
    lat, lng, seg_index = _point_at_distance(plan.segments, dist)
    seg = plan.segments[seg_index]
    return DriveSample(
        t=when,
        lat=lat,
        lng=lng,
        speed_mps=max(0.0, speed),
        distance_m=dist,
        segment_index=seg_index,
        finished=finished,
        duration_s=plan.duration_s,
        limit_kmh=seg.limit_kmh or DEFAULT_SPEED_KMH,
        source=seg.source,
        road_class=seg.road_class,
        name=seg.name,
        speed_offset_pct=plan.speed_offset_pct,
    )


def iter_samples(plan: DrivePlan, tick_s: float):
    """Yield samples from t=0 through arrival. ``tick_s`` is the playback interval."""
    if tick_s <= 0:
        raise DriveRouteError('tick_s must be positive')
    t = 0.0
    while True:
        sample = position_at(plan, t)
        yield sample
        if sample.finished:
            return
        t += tick_s


class DrivePlayer:
    """Background clock. Each tick is ``tick_s`` of *route* time, paced by the sleeper."""

    def __init__(
        self,
        on_sample: Callable[[DriveSample], None],
        on_state: Optional[Callable[[str], None]] = None,
        tick_s: float = DEFAULT_TICK_S,
        sleeper: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._on_sample = on_sample
        self._on_state = on_state
        self._tick_s = tick_s
        self._sleeper = sleeper
        self._plan: Optional[DrivePlan] = None
        self._elapsed = 0.0
        self._state = 'idle'
        self._arm = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop_thread = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def elapsed(self) -> float:
        with self._lock:
            return self._elapsed

    def load(self, plan: DrivePlan) -> None:
        with self._lock:
            self._plan = plan
            self._elapsed = 0.0
            self._state = 'idle'
            self._arm = False

    def start(self, plan: DrivePlan) -> None:
        """Begin at t=0. The worker emits the first sample so ticks stay ordered."""
        with self._lock:
            self._plan = plan
            self._elapsed = 0.0
            self._state = 'playing'
            self._arm = True
        already_running = self._thread is not None and self._thread.is_alive()
        self._ensure_thread()
        if already_running:
            self._wake.set()

    def pause(self) -> None:
        with self._lock:
            if self._state == 'playing':
                self._state = 'paused'
        self._wake.set()
        self._emit_state('paused')

    def resume(self) -> None:
        with self._lock:
            if self._plan is None:
                raise DriveRouteError('Plan a drive first')
            if self._state != 'paused':
                return
            self._state = 'playing'
        self._wake.set()
        self._emit_state('playing')

    def stop(self) -> None:
        with self._lock:
            self._state = 'idle'
            self._elapsed = 0.0
            self._arm = False
        self._wake.set()
        self._emit_state('idle')

    def close(self) -> None:
        self._stop_thread.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_thread.clear()
        self._thread = threading.Thread(target=self._run, name='geoport-drive', daemon=True)
        self._thread.start()

    def _wait(self, seconds: float) -> bool:
        if self._sleeper is not None:
            self._sleeper(max(0.0, seconds))
            return self._stop_thread.is_set()
        self._wake.wait(max(0.0, seconds))
        self._wake.clear()
        return self._stop_thread.is_set()

    def _emit_sample(self, sample: DriveSample) -> None:
        try:
            self._on_sample(sample)
        except Exception as exc:
            logger.warning(f'Drive sample handler failed: {exc}')

    def _emit_state(self, state: str) -> None:
        if self._on_state is None:
            return
        try:
            self._on_state(state)
        except Exception as exc:
            logger.debug(f'Drive state handler failed: {exc}')

    def _run(self) -> None:
        next_deadline: Optional[float] = None
        while not self._stop_thread.is_set():
            with self._lock:
                state = self._state
                tick = self._tick_s
                plan = self._plan
            if state != 'playing' or plan is None:
                next_deadline = None
                if self._wait(0.25):
                    return
                continue
            armed = False
            with self._lock:
                if self._arm and self._state == 'playing' and self._plan is not None:
                    self._arm = False
                    armed_sample = position_at(self._plan, 0.0)
                    armed = True
                else:
                    armed_sample = None
            if armed and armed_sample is not None:
                self._emit_sample(armed_sample)
                self._emit_state('playing')
                next_deadline = time.monotonic() + tick
                continue
            now = time.monotonic()
            if next_deadline is None:
                next_deadline = now + tick
            if now < next_deadline:
                if self._wait(next_deadline - now):
                    return
                with self._lock:
                    if self._state != 'playing':
                        next_deadline = None
                        continue
            with self._lock:
                if self._state != 'playing' or self._plan is None:
                    next_deadline = None
                    continue
                self._elapsed += tick
                when = self._elapsed
                plan = self._plan
            sample = position_at(plan, when)
            self._emit_sample(sample)
            if sample.finished:
                with self._lock:
                    self._state = 'finished'
                    self._elapsed = plan.duration_s
                self._emit_state('finished')
                next_deadline = None
                continue
            next_deadline += tick
            if self._sleeper is not None:
                next_deadline = time.monotonic()


class DriveService:
    """One active drive for the desktop app: plan, then play / pause / stop."""

    def __init__(
        self,
        on_sample: Callable[[DriveSample], None],
        on_state: Optional[Callable[[str], None]] = None,
        http: Any = None,
        tick_s: float = DEFAULT_TICK_S,
        sleeper: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._on_sample = on_sample
        self._http = http if http is not None else requests.Session()
        self._player = DrivePlayer(on_sample, on_state=on_state, tick_s=tick_s, sleeper=sleeper)
        self._lock = threading.Lock()
        self._raw: list[MicroEdge] = []
        self._plan: Optional[DrivePlan] = None
        self._provider = 'osrm'
        self._offset = 0.0
        self._last_speed_kmh = None
        self._user_stop = False

    @property
    def player(self) -> DrivePlayer:
        return self._player

    def plan(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        offset_pct: float = 0.0,
        provider: str = 'osrm',
        google_api_key: str = '',
    ) -> DrivePlan:
        Coordinate(latitude=start[0], longitude=start[1]).validate()
        Coordinate(latitude=end[0], longitude=end[1]).validate()
        if haversine_m(start[0], start[1], end[0], end[1]) < 15:
            raise DriveRouteError('Start and destination are the same place')
        self.stop()
        segments, used = fetch_segments(start, end, provider, google_api_key, self._http)
        plan = build_profile(segments, offset_pct, provider=used)
        with self._lock:
            self._raw = segments
            self._plan = plan
            self._provider = used
            self._offset = plan.speed_offset_pct
        self._player.load(plan)
        return plan

    def load_segments(
        self,
        segments: list[MicroEdge],
        offset_pct: float = 0.0,
        provider: str = 'simulate',
    ) -> DrivePlan:
        plan = build_profile(segments, offset_pct, provider=provider)
        with self._lock:
            self._raw = segments
            self._plan = plan
            self._provider = provider
            self._offset = plan.speed_offset_pct
        self._player.load(plan)
        return plan

    def start(self, offset_pct: Optional[float] = None) -> dict[str, Any]:
        with self._lock:
            if not self._raw or self._plan is None:
                raise DriveRouteError('Plan a drive first')
            state = self._player.state
            plan = self._plan
            if state not in ('paused', 'playing'):
                if offset_pct is not None:
                    self._offset = clamp_offset(offset_pct)
                plan = build_profile(self._raw, self._offset, provider=self._provider)
                self._plan = plan
        if state == 'paused':
            self._player.resume()
        elif state == 'playing':
            return self.status()
        else:
            self._player.start(plan)
        return self.status()

    def pause(self) -> dict[str, Any]:
        self._player.pause()
        return self.status()

    def resume(self) -> dict[str, Any]:
        self._player.resume()
        return self.status()

    def stop(self, user: bool = False) -> dict[str, Any]:
        self._user_stop = bool(user)
        self._player.stop()
        return self.status()

    def consume_user_stop(self) -> bool:
        flag = getattr(self, '_user_stop', False)
        self._user_stop = False
        return flag

    def close(self) -> None:
        self._player.close()

    def status(self) -> dict[str, Any]:
        with self._lock:
            plan = self._plan
            offset = self._offset
            provider = self._provider
        elapsed = self._player.elapsed
        duration = plan.duration_s if plan else 0.0
        return {
            'ok': True,
            'state': self._player.state,
            't': round(elapsed, 2),
            'duration_s': round(duration, 1),
            'remaining_s': round(max(0.0, duration - elapsed), 1),
            'progress': 0.0 if duration <= 0 else round(min(1.0, elapsed / duration), 5),
            'speed_offset_pct': offset,
            'provider': provider,
            'has_plan': plan is not None,
            'distance_m': round(plan.distance_m, 1) if plan else 0.0,
            'speed_summary': sources_label(plan.segments) if plan else '',
            'speed_kmh': self._last_speed_kmh,
        }


def simulate_body(body: dict[str, Any]) -> dict[str, Any]:
    """Replay an explicit route and return every emitted sample. No network."""
    points = body.get('points')
    speeds = body.get('speeds_kmh')
    if not isinstance(points, list) or not isinstance(speeds, list):
        raise DriveRouteError('points and speeds_kmh are required')
    tick = float(body.get('tick_s', 0.5))
    offset = float(body.get('speed_offset_pct', 0))
    max_samples = int(body.get('max_samples', 8000))
    if max_samples < 1:
        raise DriveRouteError('max_samples must be positive')
    segments = segments_from_points(
        points,
        speeds,
        sources=body.get('sources'),
        road_classes=body.get('road_classes'),
        names=body.get('names'),
    )
    plan = build_profile(segments, offset, provider='simulate')
    samples = []
    for sample in iter_samples(plan, tick):
        samples.append(sample.to_dict())
        if len(samples) >= max_samples:
            break
    return {
        'ok': True,
        'distance_m': round(plan.distance_m, 2),
        'duration_s': round(plan.duration_s, 2),
        'speed_offset_pct': plan.speed_offset_pct,
        'sample_count': len(samples),
        'samples': samples,
    }


def _parse_latlng(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, dict):
        raise DriveRouteError(f'{label} must be an object with lat and lng')
    try:
        lat = float(value['lat'])
        lng = float(value['lng'])
    except (KeyError, TypeError, ValueError) as exc:
        raise DriveRouteError(f'{label} needs numeric lat and lng') from exc
    Coordinate(latitude=lat, longitude=lng).validate()
    return lat, lng


def _google_key_from(body: dict[str, Any]) -> str:
    supplied = body.get('google_api_key') or ''
    if isinstance(supplied, str) and supplied.strip():
        return supplied.strip()
    return os.getenv('GEOPORT_GOOGLE_MAPS_KEY', '').strip()


def register_drive_routes(app: Any, runtime: Any, session: Any, emit: Callable[[str, dict[str, Any]], None]) -> DriveService:
    """Register ``/api/drive/*`` and push each tick through the existing location session."""
    pending: dict[str, Optional[float]] = {'lat': None, 'lng': None}
    inflight: dict[str, Any] = {'fut': None}
    push_lock = threading.Lock()

    def push_latest() -> None:
        lat = pending['lat']
        lng = pending['lng']
        if lat is None or lng is None:
            return
        future = runtime.submit(session.set_location(lat=lat, lng=lng))
        inflight['fut'] = future

        def _done(finished: Any) -> None:
            try:
                finished.result()
            except Exception as exc:
                logger.warning(f'Drive location update failed: {exc}')
            with push_lock:
                newer = pending['lat'] != lat or pending['lng'] != lng
                if newer and session.snapshot.connected:
                    push_latest()
                else:
                    inflight['fut'] = None

        future.add_done_callback(_done)

    def _device() -> Optional[dict[str, Any]]:
        snap = session.snapshot
        if not snap.connected:
            return None
        return {'udid': snap.udid, 'name': snap.device_name}

    def on_sample(sample: DriveSample) -> None:
        service._last_speed_kmh = round(sample.speed_kmh, 1)
        lat, lng = sample.lat, sample.lng
        position = app.extensions['geoport'].get('position')
        if position is not None:
            fix = position.note(lat, lng, source='drive', device=_device())
            lat, lng = fix['lat'], fix['lng']
        pushed = False
        try:
            session.update_target_coordinate(lat, lng)
        except ValueError as exc:
            logger.debug(f'Drive coordinate rejected: {exc}')
        if session.snapshot.connected:
            with push_lock:
                pending['lat'] = lat
                pending['lng'] = lng
                future = inflight['fut']
                if future is None or future.done():
                    push_latest()
                    pushed = True
        payload = sample.to_dict()
        payload['lat'] = lat
        payload['lng'] = lng
        payload['state'] = 'finished' if sample.finished else 'playing'
        payload['device'] = pushed
        emit('drive_tick', payload)

    def on_state(state: str) -> None:
        service_status = service.status()
        service_status['state'] = state
        user_stop = state == 'idle' and service.consume_user_stop()
        if state in ('finished', 'paused') or user_stop:
            service_status['follow_current'] = True
            position = app.extensions['geoport'].get('position')
            if position is not None:
                position.flush()
        emit('drive_state', service_status)

    service = DriveService(on_sample, on_state=on_state)
    app.extensions['geoport']['drive'] = service

    def _body() -> dict[str, Any]:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            raise DriveRouteError('JSON object expected')
        return data

    @app.route('/api/drive/route', methods=['POST'])
    def drive_route() -> Any:
        try:
            body = _body()
            start = _parse_latlng(body.get('start'), 'start')
            end = _parse_latlng(body.get('end'), 'end')
            offset = float(body.get('speed_offset_pct', 0))
            provider = str(body.get('provider') or 'osrm')
            plan = service.plan(
                start,
                end,
                offset_pct=offset,
                provider=provider,
                google_api_key=_google_key_from(body),
            )
            return jsonify(plan.to_public_dict())
        except DriveRouteError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), exc.status
        except ValueError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
        except Exception as exc:
            logger.error(f'/api/drive/route failed: {exc}')
            return jsonify({'ok': False, 'error': 'Could not plan a driving route'}), 502

    @app.route('/api/drive/start', methods=['POST'])
    def drive_start() -> Any:
        try:
            body = _body()
            offset = body.get('speed_offset_pct')
            status = service.start(None if offset is None else float(offset))
            return jsonify(status)
        except DriveRouteError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), exc.status
        except (TypeError, ValueError) as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400

    @app.route('/api/drive/pause', methods=['POST'])
    def drive_pause() -> Any:
        return jsonify(service.pause())

    @app.route('/api/drive/resume', methods=['POST'])
    def drive_resume() -> Any:
        try:
            return jsonify(service.resume())
        except DriveRouteError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), exc.status

    @app.route('/api/drive/stop', methods=['POST'])
    def drive_stop() -> Any:
        return jsonify(service.stop(user=True))

    @app.route('/api/drive/status', methods=['GET'])
    def drive_status() -> Any:
        return jsonify(service.status())

    @app.route('/api/drive/simulate', methods=['POST'])
    def drive_simulate() -> Any:
        try:
            return jsonify(simulate_body(_body()))
        except DriveRouteError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), exc.status
        except (TypeError, ValueError) as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400

    return service
