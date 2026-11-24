"""Drive-route timings. No phone, no network: the player emits the same samples as the model."""
from __future__ import annotations

import math
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from geoport.services.drive import (  # noqa: E402
    EARTH_RADIUS_M,
    DrivePlayer,
    anchor_endpoints,
    apply_valhalla,
    build_profile,
    decode_polyline,
    fetch_segments,
    iter_samples,
    parse_google_directions,
    parse_osrm_route,
    position_at,
    segments_from_points,
    simulate_body,
)
from geoport.services.drive import ValEdge  # noqa: E402


def _move(lat: float, lng: float, north_m: float = 0.0, east_m: float = 0.0) -> tuple[float, float]:
    lat2 = lat + (north_m / EARTH_RADIUS_M) * (180.0 / math.pi)
    cos = max(0.2, math.cos(math.radians(lat)))
    lng2 = lng + (east_m / (EARTH_RADIUS_M * cos)) * (180.0 / math.pi)
    return lat2, lng2


def _straight(speed_a: float, speed_b: float, length_a: float = 5000.0, length_b: float = 5000.0, turn: str = 'north'):
    start = (0.0, 0.0)
    mid = _move(*start, north_m=length_a)
    if turn == 'north':
        end = _move(*mid, north_m=length_b)
    else:
        end = _move(*mid, east_m=length_b)
    segments = segments_from_points([start, mid, end], [speed_a, speed_b], sources=['osrm_annotation', 'osrm_annotation'])
    return segments


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _cruise_speeds(samples, segment_index: int, segments) -> list[float]:
    seg = segments[segment_index]
    lo = seg.d0 + 0.25 * seg.length_m
    hi = seg.d0 + 0.75 * seg.length_m
    return [
        sample.speed_kmh
        for sample in samples
        if sample.segment_index == segment_index and lo <= sample.distance_m <= hi and not sample.finished
    ]


class ProfileTests(unittest.TestCase):
    def test_cruise_speed_matches_each_segment(self) -> None:
        segments = _straight(72, 36)
        plan = build_profile(segments, 0)
        samples = list(iter_samples(plan, 0.5))
        fast = _cruise_speeds(samples, 0, plan.segments)
        slow = _cruise_speeds(samples, 1, plan.segments)
        self.assertGreater(len(fast), 20)
        self.assertGreater(len(slow), 20)
        self.assertAlmostEqual(_median(fast), 72.0, delta=1.0)
        self.assertAlmostEqual(_median(slow), 36.0, delta=1.0)
        # The faster road is crossed in less time per metre.
        fast_span = [s.t for s in samples if s.segment_index == 0]
        slow_span = [s.t for s in samples if s.segment_index == 1]
        fast_rate = plan.segments[0].length_m / max(0.1, fast_span[-1] - fast_span[0])
        slow_rate = plan.segments[1].length_m / max(0.1, slow_span[-1] - slow_span[0])
        self.assertGreater(fast_rate, slow_rate * 1.4)

    def test_samples_stay_on_the_road_and_interpolate(self) -> None:
        segments = _straight(60, 60)
        plan = build_profile(segments, 0)
        samples = list(iter_samples(plan, 0.5))
        for sample in samples:
            if sample.segment_index == 0:
                self.assertAlmostEqual(sample.lng, 0.0, delta=1e-8)
                self.assertGreaterEqual(sample.lat, -1e-9)
            self.assertGreaterEqual(sample.distance_m, -1e-6)
        distances = [sample.distance_m for sample in samples]
        self.assertEqual(distances, sorted(distances))
        # A mid-route fix is between the vertices, not snapped to either end.
        seg = plan.segments[0]
        middle = next(s for s in samples if seg.d0 + 400 < s.distance_m < seg.d1 - 400)
        self.assertGreater(middle.lat, seg.lat1 + 1e-5)
        self.assertLess(middle.lat, seg.lat2 - 1e-5)
        # Consecutive fixes are a fraction of the segment, so the pin does not jump vertex to vertex.
        step = haversine_pair(samples[40], samples[41])
        self.assertLess(step, 40)
        self.assertGreater(step, 1)

    def test_starts_from_rest_and_stops(self) -> None:
        plan = build_profile(_straight(72, 72), 0)
        samples = list(iter_samples(plan, 0.25))
        early = next(sample for sample in samples if 0.4 <= sample.t <= 0.6)
        self.assertLess(early.speed_kmh, 15)
        self.assertTrue(samples[-1].finished)
        self.assertAlmostEqual(samples[-1].speed_kmh, 0.0, delta=0.1)
        self.assertAlmostEqual(samples[-1].distance_m, plan.distance_m, delta=1.0)
        covered = sum(phase.d1 - phase.d0 for phase in plan.phases)
        self.assertAlmostEqual(covered, plan.distance_m, delta=1.0)

    def test_speed_offset_scales_cruise_and_shortens_the_drive(self) -> None:
        segments = _straight(60, 60)
        plain = build_profile(segments, 0)
        quicker = build_profile(segments, 20)
        plain_samples = list(iter_samples(plain, 0.5))
        quick_samples = list(iter_samples(quicker, 0.5))
        self.assertAlmostEqual(_median(_cruise_speeds(plain_samples, 0, plain.segments)), 60.0, delta=1.0)
        self.assertAlmostEqual(_median(_cruise_speeds(quick_samples, 0, quicker.segments)), 72.0, delta=1.0)
        self.assertLess(quicker.duration_s, plain.duration_s * 0.9)

    def test_corner_slows_down_and_a_straight_join_does_not(self) -> None:
        turned = build_profile(_straight(80, 80, 3000, 3000, turn='east'), 0)
        straight = build_profile(_straight(80, 80, 3000, 3000, turn='north'), 0)
        turn_samples = list(iter_samples(turned, 0.25))
        straight_samples = list(iter_samples(straight, 0.25))
        corner_m = turned.segments[0].length_m
        near_turn = [s.speed_kmh for s in turn_samples if abs(s.distance_m - corner_m) < 40]
        near_join = [s.speed_kmh for s in straight_samples if abs(s.distance_m - straight.segments[0].length_m) < 40]
        self.assertTrue(near_turn)
        self.assertTrue(near_join)
        self.assertLess(min(near_turn), 55)
        self.assertGreater(min(near_join), 75)
        self.assertGreater(turned.duration_s, straight.duration_s)

    def test_posted_limit_overrides_osrm_estimate(self) -> None:
        segments = parse_osrm_route({
            'code': 'Ok',
            'routes': [{
                'geometry': {'coordinates': [[0.0, 0.0], [0.0, 0.01], [0.0, 0.02]]},
                'legs': [{'annotation': {
                    'distance': [1113.0, 1113.0],
                    'speed': [13.89, 13.89],
                }}],
            }],
        })
        self.assertEqual(segments[0].source, 'osrm_annotation')
        applied = apply_valhalla(segments, [
            ValEdge(length_m=1113, limit_kmh=100, speed_kmh=93, road_class='motorway', name='M3'),
            ValEdge(length_m=1113, limit_kmh=None, speed_kmh=48, road_class='residential', name='Lane'),
        ])
        self.assertTrue(applied)
        self.assertEqual(segments[0].source, 'osm_maxspeed')
        self.assertEqual(segments[0].limit_kmh, 100)
        self.assertEqual(segments[0].name, 'M3')
        self.assertEqual(segments[1].source, 'osrm_annotation')
        self.assertEqual(segments[1].road_class, 'residential')

    def test_mismatched_trace_keeps_router_speeds(self) -> None:
        segments = parse_osrm_route({
            'code': 'Ok',
            'routes': [{
                'geometry': {'coordinates': [[0.0, 0.0], [0.0, 0.01]]},
                'legs': [{'annotation': {'distance': [1000.0], 'speed': [16.67]}}],
            }],
        })
        applied = apply_valhalla(segments, [
            ValEdge(length_m=100, limit_kmh=20, speed_kmh=20, road_class='residential', name=None),
        ])
        self.assertFalse(applied)
        self.assertEqual(segments[0].source, 'osrm_annotation')
        self.assertAlmostEqual(segments[0].limit_kmh or 0, 60.0, delta=0.1)

    def test_off_network_pins_are_joined_to_the_route(self) -> None:
        segments = parse_osrm_route({
            'code': 'Ok',
            'routes': [{
                'geometry': {'coordinates': [[153.026, -27.470], [153.028, -27.472]]},
                'legs': [{'annotation': {'distance': [300.0], 'speed': [8.33]}}],
            }],
        })
        from geoport.services.drive import finalize_limits
        finalize_limits(segments)
        start = (-27.4690, 153.0250)
        end = (-27.4735, 153.0300)
        anchor_endpoints(segments, start, end)
        self.assertEqual(segments[0].source, 'approach')
        self.assertEqual((segments[0].lat1, segments[0].lng1), start)
        self.assertEqual(segments[-1].source, 'approach')
        self.assertEqual((segments[-1].lat2, segments[-1].lng2), end)
        self.assertLessEqual(segments[0].limit_kmh or 99, 30)
        plan = build_profile(segments, 0)
        self.assertEqual(plan.geometry[0], [start[0], start[1]])
        self.assertEqual(plan.geometry[-1], [end[0], end[1]])
        arrived = list(iter_samples(plan, 0.5))[-1]
        self.assertAlmostEqual(arrived.lat, end[0], delta=1e-6)
        self.assertAlmostEqual(arrived.lng, end[1], delta=1e-6)

    def test_road_class_fallback_when_nothing_else_is_known(self) -> None:
        segments = parse_osrm_route({
            'code': 'Ok',
            'routes': [{
                'geometry': {'coordinates': [[0.0, 0.0], [0.001, 0.0]]},
                'legs': [{'annotation': {'distance': [100.0], 'speed': [0]}}],
            }],
        })
        apply_valhalla(segments, [
            ValEdge(length_m=100, limit_kmh=None, speed_kmh=None, road_class='residential', name='Home'),
        ])
        from geoport.services.drive import finalize_limits
        finalize_limits(segments)
        self.assertEqual(segments[0].source, 'road_class')
        self.assertEqual(segments[0].limit_kmh, 30)

    def test_google_polyline_and_pace(self) -> None:
        # Google's published example: (38.5, -120.2), (40.7, -120.95), (43.252, -126.453).
        decoded = decode_polyline('_p~iF~ps|U_ulLnnqC_mqNvxq`@')
        self.assertEqual(len(decoded), 3)
        self.assertAlmostEqual(decoded[0][0], 38.5, delta=1e-5)
        self.assertAlmostEqual(decoded[2][1], -126.453, delta=1e-4)
        segments = parse_google_directions({
            'status': 'OK',
            'routes': [{'legs': [{'steps': [{
                'html_instructions': 'Head <b>north</b>',
                'distance': {'value': 1000},
                'duration': {'value': 50},
                'polyline': {'points': '_p~iF~ps|U_ulLnnqC_mqNvxq`@'},
            }]}]}],
        })
        self.assertGreaterEqual(len(segments), 1)
        self.assertEqual(segments[0].source, 'google_pace')
        self.assertAlmostEqual(segments[0].limit_kmh or 0, 72.0, delta=0.1)

    def test_simulate_endpoint_shape(self) -> None:
        start = (0.0, 0.0)
        mid = _move(*start, north_m=2000)
        end = _move(*mid, north_m=2000)
        body = simulate_body({
            'points': [start, mid, end],
            'speeds_kmh': [50, 100],
            'tick_s': 1.0,
            'speed_offset_pct': 0,
        })
        self.assertTrue(body['samples'][0]['t'] == 0)
        self.assertTrue(body['samples'][-1]['finished'])
        self.assertGreater(body['duration_s'], 60)
        fast = [s for s in body['samples'] if s['segment'] == 1 and not s['finished']]
        mid_fast = fast[len(fast) // 2]
        self.assertGreater(mid_fast['speed_kmh'], 90)

    def test_player_emits_the_same_positions_and_timings(self) -> None:
        plan = build_profile(_straight(70, 40, 800, 800), 0)
        emitted = []
        player = DrivePlayer(
            on_sample=emitted.append,
            tick_s=0.5,
            sleeper=lambda _seconds: None,
        )
        try:
            player.start(plan)
            deadline = time.time() + 3
            while player.state != 'finished' and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(player.state, 'finished')
        finally:
            player.close()
        expected = list(iter_samples(plan, 0.5))
        self.assertEqual(len(emitted), len(expected))
        for got, want in zip(emitted, expected):
            self.assertAlmostEqual(got.t, want.t, delta=1e-6)
            self.assertAlmostEqual(got.lat, want.lat, delta=1e-9)
            self.assertAlmostEqual(got.lng, want.lng, delta=1e-9)
            self.assertAlmostEqual(got.speed_mps, want.speed_mps, delta=1e-6)
            self.assertEqual(got.segment_index, want.segment_index)
            self.assertEqual(got.finished, want.finished)


def haversine_pair(a, b) -> float:
    from geoport.services.drive import haversine_m
    return haversine_m(a.lat, a.lng, b.lat, b.lng)


class HttpPlanningTests(unittest.TestCase):
    def test_osrm_fallback_then_valhalla(self) -> None:
        class FakeResponse:
            def __init__(self, payload, status=200):
                self._payload = payload
                self.status_code = status

            def json(self):
                return self._payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f'HTTP {self.status_code}')

        class FakeHttp:
            def __init__(self):
                self.gets = []

            def get(self, url, **kwargs):
                self.gets.append(url)
                if 'openstreetmap.de' in url:
                    raise TimeoutError('primary down')
                return FakeResponse({
                    'code': 'Ok',
                    'routes': [{
                        'geometry': {'coordinates': [[153.0, -27.0], [153.001, -27.0], [153.002, -27.0]]},
                        'legs': [{'annotation': {
                            'distance': [100.0, 100.0],
                            'speed': [8.0, 8.0],
                        }}],
                    }],
                })

            def post(self, url, **kwargs):
                return FakeResponse({
                    'units': 'kilometers',
                    'edges': [
                        {'length': 0.1, 'speed_limit': 60, 'speed': 55, 'road_class': 'primary', 'names': ['Ann St']},
                        {'length': 0.1, 'speed_limit': 0, 'speed': 28, 'road_class': 'residential', 'names': ['Lane']},
                    ],
                })

        http = FakeHttp()
        segments, provider = fetch_segments((-27.0, 153.0), (-27.0, 153.002), 'osrm', '', http)
        self.assertEqual(provider, 'osrm')
        self.assertEqual(len(http.gets), 2)
        self.assertEqual(segments[0].source, 'osm_maxspeed')
        self.assertEqual(segments[0].limit_kmh, 60)
        self.assertEqual(segments[0].name, 'Ann St')
        plan = build_profile(segments, 0, provider=provider)
        self.assertGreater(plan.duration_s, 0)
        self.assertIn('posted limit', plan.to_public_dict()['speed_summary'])

    def test_flask_simulate_and_start(self) -> None:
        from geoport.runtime import get_runtime
        from geoport.web.app import create_app

        runtime = get_runtime()
        try:
            app, _socketio = create_app(runtime=runtime)
            client = app.test_client()
            start = (0.0, 0.0)
            mid = _move(*start, north_m=1500)
            end = _move(*mid, north_m=1500)
            response = client.post('/api/drive/simulate', json={
                'points': [list(start), list(mid), list(end)],
                'speeds_kmh': [40, 80],
                'tick_s': 1,
            })
            self.assertEqual(response.status_code, 200, response.get_json())
            body = response.get_json()
            self.assertGreater(len(body['samples']), 5)
            self.assertTrue(body['samples'][-1]['finished'])
            self.assertLess(body['samples'][1]['speed_kmh'], 30)

            page = client.get('/')
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn('Plan drive', html)
            self.assertIn('driveStartInput', html)
            self.assertIn('Use current pin', html)
            missing = client.post('/api/drive/start', json={})
            self.assertEqual(missing.status_code, 400)
            bad = client.post('/api/drive/route', json={'start': {'lat': 1}, 'end': {'lat': 2, 'lng': 3}})
            self.assertEqual(bad.status_code, 400)
            self.assertIn('error', bad.get_json())
        finally:
            drive = app.extensions['geoport'].get('drive')
            if drive is not None:
                drive.close()
            runtime.shutdown(timeout=2.0)


if __name__ == '__main__':
    unittest.main()
