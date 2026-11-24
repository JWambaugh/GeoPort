"""Stop signs and traffic signals along a driving route.

Nodes come from the Overpass API. A timeout or a bad response leaves the
route drivable with no stops; the plan says so. Results are cached per
route shape so a second plan does not ask again.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from geoport.config import logger
from geoport.services.drive import USER_AGENT, MicroEdge, bearing_deg, haversine_m, turn_angle_deg

OVERPASS_URLS = [
    os.getenv('GEOPORT_OVERPASS_URL', 'https://overpass-api.de/api/interpreter'),
    os.getenv('GEOPORT_OVERPASS_FALLBACK_URL', 'https://overpass.kumi.systems/api/interpreter'),
]
SNAP_M = 15.0
SIGNAL_CLUSTER_M = 25.0
STOP_RED_PROB = 0.45
SIGNAL_WAIT_MIN_S = 5.0
SIGNAL_WAIT_MAX_S = 45.0
STOP_DWELL_MIN_S = 2.0
STOP_DWELL_MAX_S = 4.0
_CACHE_TTL_S = 15 * 60
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CACHE_LOCK = threading.Lock()


@dataclass
class PlannedStop:
    kind: str
    distance_m: float
    lat: float
    lng: float
    dwell_s: float = 0.0
    red: Optional[bool] = None
    slow: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            'kind': self.kind,
            'lat': round(self.lat, 6),
            'lng': round(self.lng, 6),
            'distance_m': round(self.distance_m, 1),
            'dwell_s': round(self.dwell_s, 2),
            'red': self.red,
        }


def expected_signal_delay_s(signal_count: int) -> float:
    """Mean wait if each signal is red with probability 0.45 for a uniform 5–45 s."""
    mean_red = (SIGNAL_WAIT_MIN_S + SIGNAL_WAIT_MAX_S) / 2.0
    return signal_count * STOP_RED_PROB * mean_red


def parse_control_elements(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[int, float]]:
    """Return candidate nodes and a map of node id to parent-way bearing, when present."""
    nodes: list[dict[str, Any]] = []
    ways: list[dict[str, Any]] = []
    for element in payload.get('elements') or []:
        if element.get('type') == 'node' and element.get('lat') is not None:
            kind = _kind(element.get('tags') or {})
            if kind is None:
                continue
            tags = element.get('tags') or {}
            nodes.append({
                'id': element.get('id'),
                'lat': float(element['lat']),
                'lng': float(element['lon']),
                'kind': kind,
                'direction': (tags.get('direction') or '').strip().lower() or None,
            })
        elif element.get('type') == 'way':
            ways.append(element)
    bearings: dict[int, float] = {}
    for way in ways:
        geom = way.get('geometry') or []
        if len(geom) < 2:
            continue
        bearing = bearing_deg(geom[0]['lat'], geom[0]['lon'], geom[-1]['lat'], geom[-1]['lon'])
        for node_id in way.get('nodes') or []:
            bearings.setdefault(int(node_id), bearing)
    return nodes, bearings


def _kind(tags: dict[str, Any]) -> Optional[str]:
    highway = tags.get('highway')
    if highway == 'stop':
        return 'stop_sign'
    if highway == 'traffic_signals' or tags.get('crossing') == 'traffic_signals':
        return 'signal'
    if highway == 'give_way':
        return 'give_way'
    return None


def applies_to_travel(direction: Optional[str], way_bearing: Optional[float], route_bearing: float) -> bool:
    """Keep a node unless its direction tag faces the other way along a known way."""
    if direction not in ('forward', 'backward'):
        return True
    if way_bearing is None:
        return True
    aligned = turn_angle_deg(way_bearing, route_bearing) < 90.0
    going = 'forward' if aligned else 'backward'
    return direction == going


def snap_controls(
    nodes: list[dict[str, Any]],
    segments: list[MicroEdge],
    bearings_by_id: Optional[dict[int, float]] = None,
    snap_m: float = SNAP_M,
) -> list[dict[str, Any]]:
    """Project nodes onto the route. Drop anything farther than ``snap_m`` or facing away."""
    if not segments:
        return []
    _stamp_distance(segments)
    bearings_by_id = bearings_by_id or {}
    snapped: list[dict[str, Any]] = []
    for node in nodes:
        hit = _nearest_on_route(node['lat'], node['lng'], segments)
        if hit is None or hit[0] > snap_m:
            continue
        distance_m, lat, lng, route_bearing = hit[1], hit[2], hit[3], hit[4]
        way_bearing = bearings_by_id.get(int(node['id'])) if node.get('id') is not None else None
        if not applies_to_travel(node.get('direction'), way_bearing, route_bearing):
            continue
        snapped.append({
            'kind': node['kind'],
            'distance_m': distance_m,
            'lat': lat,
            'lng': lng,
        })
    snapped.sort(key=lambda item: item['distance_m'])
    return _dedupe(snapped)


def _dedupe(snapped: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for item in snapped:
        cluster = SIGNAL_CLUSTER_M if item['kind'] == 'signal' else 8.0
        if any(abs(item['distance_m'] - prev['distance_m']) < cluster and prev['kind'] == item['kind'] for prev in kept):
            continue
        kept.append(item)
    return kept


def _stamp_distance(segments: list[MicroEdge]) -> None:
    cursor = 0.0
    for seg in segments:
        if seg.d1 <= seg.d0:
            seg.d0 = cursor
            seg.d1 = cursor + seg.length_m
        cursor = seg.d1


def _nearest_on_route(
    lat: float,
    lng: float,
    segments: list[MicroEdge],
) -> Optional[tuple[float, float, float, float, float]]:
    best: Optional[tuple[float, float, float, float, float]] = None
    cos = max(0.2, __import__('math').cos(__import__('math').radians(lat)))
    m_per_deg = 6371000.0 * 3.141592653589793 / 180.0
    for seg in segments:
        along_m, frac, off_m = _project(lat, lng, seg, cos, m_per_deg)
        if best is None or off_m < best[0]:
            point_lat = seg.lat1 + (seg.lat2 - seg.lat1) * frac
            point_lng = seg.lng1 + (seg.lng2 - seg.lng1) * frac
            route_bearing = bearing_deg(seg.lat1, seg.lng1, seg.lat2, seg.lng2)
            best = (off_m, seg.d0 + along_m, point_lat, point_lng, route_bearing)
    return best


def _project(lat: float, lng: float, seg: MicroEdge, cos: float, m_per_deg: float) -> tuple[float, float, float]:
    slat = (seg.lat2 - seg.lat1) * m_per_deg
    slng = (seg.lng2 - seg.lng1) * m_per_deg * cos
    plat = (lat - seg.lat1) * m_per_deg
    plng = (lng - seg.lng1) * m_per_deg * cos
    length_sq = slat * slat + slng * slng
    if length_sq < 1e-6:
        return 0.0, 0.0, haversine_m(lat, lng, seg.lat1, seg.lng1)
    frac = max(0.0, min(1.0, (plat * slat + plng * slng) / length_sq))
    closest_lat = seg.lat1 + (seg.lat2 - seg.lat1) * frac
    closest_lng = seg.lng1 + (seg.lng2 - seg.lng1) * frac
    along = frac * seg.length_m
    return along, frac, haversine_m(lat, lng, closest_lat, closest_lng)


def assign_dwells(snapped: list[dict[str, Any]], rng: Any) -> list[PlannedStop]:
    planned: list[PlannedStop] = []
    for item in snapped:
        if item['kind'] == 'give_way':
            planned.append(PlannedStop(
                kind='give_way',
                distance_m=item['distance_m'],
                lat=item['lat'],
                lng=item['lng'],
                dwell_s=0.0,
                slow=True,
            ))
            continue
        if item['kind'] == 'stop_sign':
            planned.append(PlannedStop(
                kind='stop_sign',
                distance_m=item['distance_m'],
                lat=item['lat'],
                lng=item['lng'],
                dwell_s=rng.uniform(STOP_DWELL_MIN_S, STOP_DWELL_MAX_S),
            ))
            continue
        red = rng.random() < STOP_RED_PROB
        planned.append(PlannedStop(
            kind='signal',
            distance_m=item['distance_m'],
            lat=item['lat'],
            lng=item['lng'],
            dwell_s=rng.uniform(SIGNAL_WAIT_MIN_S, SIGNAL_WAIT_MAX_S) if red else 0.0,
            red=red,
            slow=not red,
        ))
    return planned


def summarize_stops(stops: list[PlannedStop], available: bool, note: Optional[str]) -> dict[str, Any]:
    signs = sum(1 for stop in stops if stop.kind == 'stop_sign')
    signals = sum(1 for stop in stops if stop.kind == 'signal')
    realized_signal = sum(stop.dwell_s for stop in stops if stop.kind == 'signal')
    return {
        'stop_signs': signs,
        'signals': signals,
        'available': available,
        'note': note,
        'expected_signal_s': round(expected_signal_delay_s(signals), 1),
        'realized_signal_s': round(realized_signal, 1),
    }


def eta_seconds(playback_duration_s: float, summary: dict[str, Any]) -> float:
    """Playback time with each signal's rolled wait replaced by the expected wait."""
    realized = float(summary.get('realized_signal_s') or 0.0)
    expected = float(summary.get('expected_signal_s') or 0.0)
    return max(0.0, playback_duration_s - realized + expected)


def fetch_route_controls(segments: list[MicroEdge], http: Any) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Snap OSM controls onto ``segments``. The note is set when Overpass could not be used."""
    _stamp_distance(segments)
    key = _cache_key(segments)
    cached = _cache_get(key)
    if cached is not None:
        return snap_controls(cached[0], segments, cached[1]), None
    coords = _sample_coords(segments)
    if len(coords) < 2:
        return [], None
    query = _overpass_query(coords)
    payload = None
    last_error = 'no response'
    for url in [item for item in OVERPASS_URLS if item]:
        try:
            response = http.post(
                url,
                data={'data': query},
                headers={'User-Agent': USER_AGENT},
                timeout=(3.05, 6),
            )
            response.raise_for_status()
            payload = response.json()
            break
        except Exception as exc:
            last_error = str(exc)
            logger.info(f'Overpass stop lookup skipped ({url}): {exc}')
    if payload is None:
        return [], 'Stop signs unavailable (Overpass did not respond)'
    del last_error
    nodes, bearings = parse_control_elements(payload if isinstance(payload, dict) else {})
    _cache_put(key, (nodes, bearings))
    return snap_controls(nodes, segments, bearings), None


def _sample_coords(segments: list[MicroEdge]) -> list[tuple[float, float]]:
    coords = [(segments[0].lat1, segments[0].lng1)]
    for seg in segments:
        coords.append((seg.lat2, seg.lng2))
    if len(coords) <= 60:
        return coords
    step = max(1, len(coords) // 60)
    sampled = coords[::step]
    if sampled[-1] != coords[-1]:
        sampled.append(coords[-1])
    return sampled


def _overpass_query(coords: list[tuple[float, float]]) -> str:
    flat = ','.join(f'{lat:.6f},{lng:.6f}' for lat, lng in coords)
    around = f'around:15,{flat}'
    return (
        '[out:json][timeout:8];('
        f'node({around})["highway"="stop"];'
        f'node({around})["highway"="traffic_signals"];'
        f'node({around})["highway"="give_way"];'
        f'node({around})["crossing"="traffic_signals"];'
        ');out body;'
    )


def _cache_key(segments: list[MicroEdge]) -> str:
    coords = _sample_coords(segments)
    raw = ';'.join(f'{lat:.4f},{lng:.4f}' for lat, lng in coords)
    return hashlib.sha1(raw.encode()).hexdigest()


def _cache_get(key: str) -> Optional[tuple[list[dict[str, Any]], dict[int, float]]]:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is None:
            return None
        stored_at, value = hit
        if time.monotonic() - stored_at > _CACHE_TTL_S:
            _CACHE.pop(key, None)
            return None
        return value


def _cache_put(key: str, value: tuple[list[dict[str, Any]], dict[int, float]]) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) > 64:
            _CACHE.clear()
        _CACHE[key] = (time.monotonic(), value)
