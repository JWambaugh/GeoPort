"""Stop signs and traffic signals along a driving route.

Nodes come from the Overpass API. A timeout or a bad response leaves the
route drivable with no stops; the plan says so. Results are cached per
route shape so a second plan does not ask again.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from geoport.config import logger
from geoport.services.drive import USER_AGENT, MicroEdge, bearing_deg, haversine_m, turn_angle_deg

def _mirror_list() -> list[str]:
    raw = os.getenv('GEOPORT_OVERPASS_URLS', '').strip()
    if raw:
        mirrors = [item.strip() for item in raw.split(',') if item.strip()]
    else:
        # overpass.openstreetmap.fr answered a live probe on 2026-11-24.
        # de returned 502, kumi and private.coffee timed out, mail.ru returned 504,
        # and overpass.osm.jp did not resolve. They stay in the rotation anyway.
        mirrors = [
            os.getenv('GEOPORT_OVERPASS_URL', 'https://overpass.openstreetmap.fr/api/interpreter'),
            'https://overpass-api.de/api/interpreter',
            'https://overpass.kumi.systems/api/interpreter',
            'https://overpass.private.coffee/api/interpreter',
            'https://maps.mail.ru/osm/tools/overpass/api/interpreter',
        ]
    seen = []
    for mirror in mirrors:
        if mirror not in seen:
            seen.append(mirror)
    return seen


OVERPASS_MIRRORS = _mirror_list()
REQUEST_TIMEOUT = (3.05, 25.0)
CHUNK_M = 4000.0
PLAN_WAIT_S = float(os.getenv('GEOPORT_STOPS_BUDGET', '3.5'))
_BACKOFF_S = (0.25, 0.5, 1.0, 1.6)
SNAP_M = 15.0
SIGNAL_CLUSTER_M = 25.0
STOP_RED_PROB = 0.45
SIGNAL_WAIT_MIN_S = 5.0
SIGNAL_WAIT_MAX_S = 45.0
STOP_DWELL_MIN_S = 2.0
STOP_DWELL_MAX_S = 4.0
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


@dataclass
class RouteChunk:
    id: int
    d0: float
    d1: float
    segments: list[MicroEdge]
    bbox: tuple[float, float, float, float]

    def public(self, state: str, stops: int = 0, error: Optional[str] = None) -> dict[str, Any]:
        return {
            'id': self.id,
            'bbox': [round(value, 2) for value in self.bbox],
            'state': state,
            'stops': stops,
            'error': error,
        }


def split_route_chunks(segments: list[MicroEdge], target_m: float = CHUNK_M) -> list[RouteChunk]:
    """Cut the route into pieces of about 4 km so each Overpass query stays small."""
    _stamp_distance(segments)
    chunks: list[RouteChunk] = []
    current: list[MicroEdge] = []
    length = 0.0
    for seg in segments:
        current.append(seg)
        length += seg.length_m
        if length >= target_m:
            chunks.append(_make_chunk(len(chunks), current))
            current = []
            length = 0.0
    if current:
        chunks.append(_make_chunk(len(chunks), current))
    return chunks


def _make_chunk(index: int, segments: list[MicroEdge]) -> RouteChunk:
    lats = [seg.lat1 for seg in segments] + [segments[-1].lat2]
    lngs = [seg.lng1 for seg in segments] + [segments[-1].lng2]
    pad = 0.0003
    bbox = (min(lats) - pad, min(lngs) - pad, max(lats) + pad, max(lngs) + pad)
    return RouteChunk(id=index, d0=segments[0].d0, d1=segments[-1].d1, segments=list(segments), bbox=bbox)


def bbox_cache_key(bbox: tuple[float, float, float, float]) -> str:
    return '_'.join(f'{round(value, 2):.2f}' for value in bbox)


def query_mirrors(
    http: Any,
    query: str,
    mirrors: list[str],
    backoff: tuple[float, ...] = _BACKOFF_S,
    sleeper: Any = time.sleep,
) -> dict[str, Any]:
    """Try mirrors in order. A 429, 5xx, or timeout waits, then the next mirror is used."""
    last_error: Exception = RuntimeError('no Overpass mirror configured')
    for attempt, url in enumerate(mirrors):
        try:
            response = http.post(
                url,
                data={'data': query},
                headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'},
                timeout=REQUEST_TIMEOUT,
            )
            status = int(getattr(response, 'status_code', 200) or 200)
            if status == 429 or status >= 500:
                raise RuntimeError(f'HTTP {status}')
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError('Overpass returned a non-object')
            return payload
        except Exception as exc:
            last_error = exc
            logger.info(f'Overpass {url} failed: {exc}')
            if attempt + 1 < len(mirrors) and backoff:
                delay = backoff[min(attempt, len(backoff) - 1)]
                if delay > 0:
                    sleeper(delay)
    raise RuntimeError(str(last_error))


def _bbox_query(bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    box = f'{south:.5f},{west:.5f},{north:.5f},{east:.5f}'
    return (
        '[out:json][timeout:25];('
        f'node({box})["highway"="stop"];'
        f'node({box})["highway"="traffic_signals"];'
        f'node({box})["highway"="give_way"];'
        f'node({box})["crossing"="traffic_signals"];'
        ');out body;'
    )


def _read_disk_cache(cache_dir: Path, key: str) -> Optional[list[dict[str, Any]]]:
    try:
        raw = json.loads((cache_dir / f'{key}.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, list) else None


def _write_disk_cache(cache_dir: Path, key: str, nodes: list[dict[str, Any]]) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f'{key}.json'
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(nodes), encoding='utf-8')
        temporary.replace(path)
    except OSError as exc:
        logger.debug(f'Could not cache Overpass chunk: {exc}')


class StopLookup:
    """Fetch stop chunks without holding up the start of a drive."""

    def __init__(
        self,
        segments: list[MicroEdge],
        http: Any,
        cache_dir: Path,
        mirrors: Optional[list[str]] = None,
        sleeper: Any = time.sleep,
    ) -> None:
        _stamp_distance(segments)
        self.segments = segments
        self.http = http
        self.cache_dir = Path(cache_dir)
        self.mirrors = list(mirrors if mirrors is not None else OVERPASS_MIRRORS)
        self.sleeper = sleeper
        self.chunks = split_route_chunks(segments)
        self._lock = threading.Lock()
        self._states = {chunk.id: 'pending' for chunk in self.chunks}
        self._errors: dict[int, Optional[str]] = {chunk.id: None for chunk in self.chunks}
        self._nodes: dict[int, list[dict[str, Any]]] = {chunk.id: [] for chunk in self.chunks}
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._on_update: Optional[Any] = None
        self._load_cached()

    def start(self, on_update: Optional[Any] = None) -> None:
        self._on_update = on_update
        if all(state != 'pending' for state in self._states.values()):
            self._finished.set()
            if on_update is not None:
                self._notify()
            return
        self._thread = threading.Thread(target=self._run, name='geoport-stops', daemon=True)
        self._thread.start()

    def wait(self, budget_s: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if budget_s > 0 and not self._finished.is_set():
            self._finished.wait(budget_s)
        return self.snapped(), self.lookup_state()

    def cancel(self) -> None:
        self._stop.set()

    def join(self, timeout: float = 2.0) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def snapped(self) -> list[dict[str, Any]]:
        with self._lock:
            nodes = [node for chunk_nodes in self._nodes.values() for node in chunk_nodes]
        return snap_controls(nodes, self.segments, {})

    def lookup_state(self) -> dict[str, Any]:
        with self._lock:
            chunks = [
                chunk.public(self._states[chunk.id], len(self._nodes[chunk.id]), self._errors[chunk.id])
                for chunk in self.chunks
            ]
            states = [item['state'] for item in chunks]
        if not states or all(state in ('ok', 'cached') for state in states):
            overall = 'complete'
        elif all(state == 'failed' for state in states):
            overall = 'failed'
        elif any(state == 'pending' for state in states) and not any(state in ('ok', 'cached') for state in states):
            overall = 'pending'
        else:
            overall = 'partial'
        note = None
        if overall == 'failed':
            note = 'Stop signs unavailable (Overpass did not respond)'
        elif overall in ('pending', 'partial'):
            note = 'Still looking up stop signs'
        return {'state': overall, 'chunks': chunks, 'note': note}

    def _load_cached(self) -> None:
        for chunk in self.chunks:
            cached = _read_disk_cache(self.cache_dir, bbox_cache_key(chunk.bbox))
            if cached is None:
                continue
            self._nodes[chunk.id] = cached
            self._states[chunk.id] = 'cached'

    def _run(self) -> None:
        pending = [chunk for chunk in self.chunks if self._states[chunk.id] == 'pending']
        for index, chunk in enumerate(pending):
            if self._stop.is_set():
                break
            if not self.mirrors:
                with self._lock:
                    self._states[chunk.id] = 'failed'
                    self._errors[chunk.id] = 'no Overpass mirror configured'
                self._notify()
                continue
            start = index % len(self.mirrors)
            mirrors = self.mirrors[start:] + self.mirrors[:start]
            try:
                payload = query_mirrors(self.http, _bbox_query(chunk.bbox), mirrors, sleeper=self.sleeper)
                nodes, _bearings = parse_control_elements(payload)
                _write_disk_cache(self.cache_dir, bbox_cache_key(chunk.bbox), nodes)
                with self._lock:
                    self._nodes[chunk.id] = nodes
                    self._states[chunk.id] = 'ok'
                    self._errors[chunk.id] = None
            except Exception as exc:
                with self._lock:
                    self._states[chunk.id] = 'failed'
                    self._errors[chunk.id] = str(exc)
            self._notify()
        self._finished.set()
        self._notify()

    def _notify(self) -> None:
        if self._on_update is None:
            return
        try:
            self._on_update(self.snapped(), self.lookup_state())
        except Exception as exc:
            logger.debug(f'Stop update listener failed: {exc}')


def fetch_route_controls(
    segments: list[MicroEdge],
    http: Any,
    cache_dir: Optional[Path] = None,
    mirrors: Optional[list[str]] = None,
    sleeper: Any = time.sleep,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Block until every chunk finishes. Short routes and tests use this."""
    from geoport.config import get_geoport_state_dir
    lookup = StopLookup(
        segments,
        http,
        cache_dir or (get_geoport_state_dir() / 'stops_cache'),
        mirrors=mirrors,
        sleeper=sleeper,
    )
    lookup.start()
    snapped, state = lookup.wait(120)
    lookup.join(1)
    if state['state'] == 'complete':
        return snapped, None
    return snapped, state.get('note') or 'Stop signs unavailable (Overpass did not respond)'


