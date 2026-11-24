"""Drive-route timings. No phone, no network: the player emits the same samples as the model."""
from __future__ import annotations

import math
import os
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
        os.environ['GEOPORT_AUTOCONNECT'] = '0'
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
            self.assertIn('Current location', html)
            self.assertIn('centerOnCurrentLocation', html)
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


class StopAndReconnectTests(unittest.TestCase):
    def test_overpass_nodes_are_snapped_filtered_and_clustered(self) -> None:
        import tempfile
        from geoport.services.stops import fetch_route_controls, parse_control_elements, snap_controls
        segments = _straight(50, 50, 1000, 1000)
        on_route = _move(0, 0, north_m=400)
        beside = _move(on_route[0], on_route[1], east_m=8)
        far = _move(on_route[0], on_route[1], east_m=80)
        signal_a = _move(0, 0, north_m=700)
        signal_b = _move(0, 0, north_m=715)
        backward = _move(0, 0, north_m=900)
        payload = {'elements': [
            {'type': 'node', 'id': 1, 'lat': beside[0], 'lon': beside[1], 'tags': {'highway': 'stop'}},
            {'type': 'node', 'id': 2, 'lat': far[0], 'lon': far[1], 'tags': {'highway': 'traffic_signals'}},
            {'type': 'node', 'id': 3, 'lat': signal_a[0], 'lon': signal_a[1], 'tags': {'highway': 'traffic_signals'}},
            {'type': 'node', 'id': 4, 'lat': signal_b[0], 'lon': signal_b[1], 'tags': {'highway': 'traffic_signals'}},
            {'type': 'node', 'id': 5, 'lat': backward[0], 'lon': backward[1], 'tags': {'highway': 'stop', 'direction': 'forward'}},
            {'type': 'way', 'nodes': [5], 'geometry': [
                {'lat': backward[0], 'lon': backward[1]},
                {'lat': backward[0] - 0.01, 'lon': backward[1]},
            ]},
        ]}
        nodes, bearings = parse_control_elements(payload)
        snapped = snap_controls(nodes, segments, bearings)
        kinds = [item['kind'] for item in snapped]
        self.assertEqual(kinds.count('stop_sign'), 1)
        self.assertEqual(kinds.count('signal'), 1)
        self.assertNotIn('give_way', kinds)

        class Boom:
            def post(self, *args, **kwargs):
                raise TimeoutError('overpass down')

        with tempfile.TemporaryDirectory() as tmp:
            found, note = fetch_route_controls(segments, Boom(), cache_dir=Path(tmp), sleeper=lambda _s: None)
        self.assertEqual(found, [])
        self.assertIn('unavailable', note or '')

        class FakeResponse:
            def __init__(self, body):
                self._body = body
            def raise_for_status(self):
                return None
            def json(self):
                return self._body

        calls = {'n': 0}

        class FakeHttp:
            def post(self, *args, **kwargs):
                calls['n'] += 1
                return FakeResponse(payload)

        with tempfile.TemporaryDirectory() as tmp:
            first, first_note = fetch_route_controls(
                segments, FakeHttp(), cache_dir=Path(tmp), sleeper=lambda _s: None,
            )
            second, second_note = fetch_route_controls(
                segments, FakeHttp(), cache_dir=Path(tmp), sleeper=lambda _s: None,
            )
        self.assertIsNone(first_note)
        self.assertIsNone(second_note)
        self.assertEqual(calls['n'], 1)
        self.assertEqual(len(first), len(second))

    def test_stop_sign_dwells_and_green_signal_does_not(self) -> None:
        from geoport.services.stops import PlannedStop, eta_seconds
        segments = _straight(72, 72, 2000, 2000)
        plain = build_profile(segments, 0)
        stop = PlannedStop(kind='stop_sign', distance_m=800, lat=0, lng=0, dwell_s=3.0)
        red = PlannedStop(kind='signal', distance_m=1400, lat=0, lng=0, dwell_s=8.0, red=True)
        summary = {'stop_signs': 1, 'signals': 1, 'realized_signal_s': 8.0, 'expected_signal_s': 11.25}
        plan = build_profile(segments, 0, stops=[stop, red], stops_summary=summary)
        self.assertGreater(plan.duration_s, plain.duration_s + 9)
        self.assertAlmostEqual(plan.eta_s, plan.duration_s - 8.0 + 11.25, delta=0.2)
        samples = list(iter_samples(plan, 0.25))
        held = [sample for sample in samples if abs(sample.distance_m - 800) < 25 and sample.speed_kmh < 1.2]
        self.assertGreater(len(held), 8)
        self.assertGreater(held[-1].t - held[0].t, 2.0)
        public = plan.to_public_dict()
        self.assertEqual(public['stops']['stop_signs'], 1)
        self.assertEqual(public['stops']['signals'], 1)
        green = PlannedStop(kind='signal', distance_m=800, lat=0, lng=0, dwell_s=0.0, red=False, slow=True)
        flowing = build_profile(segments, 0, stops=[green], stops_summary={'realized_signal_s': 0, 'expected_signal_s': 11.25})
        near = [sample for sample in iter_samples(flowing, 0.25) if abs(sample.distance_m - 800) < 20]
        self.assertTrue(near)
        self.assertGreater(min(sample.speed_kmh for sample in near), 8)
        del eta_seconds

    def test_device_choice_prefers_usb_and_reconnect_rules(self) -> None:
        from geoport.services.autoconnect import choose_device, should_reconnect
        usb = {'Identifier': 'U1', 'ConnectionType': 'USB', 'ProductVersion': '18.0'}
        wifi = {'Identifier': 'U1', 'ConnectionType': 'Network', 'wifiAddress': '10.0.0.8'}
        chosen = choose_device({'U1': {'USB': [usb], 'Wifi': [wifi]}, 'U2': {'Wifi': [wifi]}}, 'U1')
        self.assertEqual(chosen['ConnectionType'], 'USB')
        wifi_only = choose_device({'U1': {'Wifi': [wifi]}}, 'U1')
        self.assertEqual(wifi_only['ConnectionType'], 'Network')
        self.assertIsNone(choose_device({'U1': {'Wifi': [wifi]}, 'U2': {'USB': [usb]}}, None))
        only = choose_device({'U9': {'Wifi': [wifi]}}, None)
        self.assertEqual(only['Identifier'], 'U1')
        self.assertFalse(should_reconnect(connected=True, user_hold=False, has_position=True, drive_state='playing', has_last_udid=True, startup=False))
        self.assertFalse(should_reconnect(connected=False, user_hold=True, has_position=True, drive_state='playing', has_last_udid=True, startup=True))
        self.assertTrue(should_reconnect(connected=False, user_hold=False, has_position=True, drive_state='idle', has_last_udid=False, startup=False))
        self.assertTrue(should_reconnect(connected=False, user_hold=False, has_position=False, drive_state='playing', has_last_udid=False, startup=False))
        self.assertTrue(should_reconnect(connected=False, user_hold=False, has_position=False, drive_state='idle', has_last_udid=False, startup=True))
        self.assertFalse(should_reconnect(connected=False, user_hold=False, has_position=False, drive_state='idle', has_last_udid=False, startup=False))

    def test_mirrors_rotate_chunks_split_and_failures_are_not_cached(self) -> None:
        import tempfile
        from geoport.services.stops import REQUEST_TIMEOUT, StopLookup, _BACKOFF_S, _bbox_query, query_mirrors, split_route_chunks
        route = _straight(80, 80, 5000, 5000)
        self.assertGreaterEqual(len(split_route_chunks(route)), 2)

        class Response:
            def __init__(self, status, body=None):
                self.status_code = status
                self._body = body or {'elements': []}
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f'HTTP {self.status_code}')
            def json(self):
                return self._body

        seen = []

        class Rotating:
            def post(self, url, **kwargs):
                seen.append(url)
                if url.endswith('/bad'):
                    return Response(502)
                if url.endswith('/slow'):
                    raise TimeoutError('timed out')
                return Response(200, {'elements': []})

        payload = query_mirrors(
            Rotating(),
            '[out:json][timeout:25];',
            ['http://mirror/bad', 'http://mirror/slow', 'http://mirror/good'],
            backoff=(0, 0, 0),
            sleeper=lambda _s: None,
        )
        self.assertEqual(payload, {'elements': []})
        self.assertEqual(seen, ['http://mirror/bad', 'http://mirror/slow', 'http://mirror/good'])
        self.assertEqual(REQUEST_TIMEOUT[1], 25.0)
        self.assertGreaterEqual(_BACKOFF_S[-1], 1.0)
        self.assertIn('[timeout:25]', _bbox_query((0.0, 0.0, 0.1, 0.1)))

        with tempfile.TemporaryDirectory() as tmp:
            class Fail:
                def post(self, url, **kwargs):
                    return Response(429)
            lookup = StopLookup(route, Fail(), Path(tmp), mirrors=['http://mirror/bad'], sleeper=lambda _s: None)
            lookup.start()
            lookup.wait(2)
            lookup.join(1)
            self.assertEqual(lookup.lookup_state()['state'], 'failed')
            self.assertEqual(list(Path(tmp).glob('*.json')), [])

    def test_late_stop_ahead_is_inserted_without_moving_the_car(self) -> None:
        import random
        from geoport.services.drive import DriveService
        segments = _straight(60, 60, 4000, 4000)
        plan = build_profile(segments, 0)
        service = DriveService(lambda _sample: None, sleeper=lambda _s: None)
        try:
            service._raw = segments
            service._offset = 0
            service._provider = 'osrm'
            service._plan = plan
            service._player.load(plan)
            service._player._elapsed = 25.0
            before = position_at(plan, 25.0)
            service._stop_rng = random.Random(3)
            service._planned_stops = []
            service._latest_snapped = [
                {'kind': 'stop_sign', 'distance_m': 80, 'lat': 0, 'lng': 0},
                {'kind': 'stop_sign', 'distance_m': 5500, 'lat': 0.05, 'lng': 0},
            ]
            service._latest_lookup = {
                'state': 'partial',
                'chunks': [
                    {'id': 0, 'state': 'ok', 'bbox': [0, 0, 1, 1], 'stops': 1, 'error': None},
                    {'id': 1, 'state': 'pending', 'bbox': [1, 0, 2, 1], 'stops': 0, 'error': None},
                ],
                'note': 'Still looking up stop signs',
            }
            service._lock.acquire()
            try:
                updated = service._rebuild(initial=False)
            finally:
                service._lock.release()
            after = position_at(updated, 25.0)
            self.assertAlmostEqual(after.distance_m, before.distance_m, delta=8)
            self.assertAlmostEqual(after.lat, before.lat, delta=1e-4)
            distances = [stop.distance_m for stop in service._planned_stops]
            self.assertTrue(any(distance > 5000 for distance in distances))
            self.assertFalse(any(distance < 200 for distance in distances))
            self.assertEqual(updated.stops_summary['lookup']['state'], 'partial')
            held = [
                sample for sample in iter_samples(updated, 0.5)
                if abs(sample.distance_m - 5500) < 40 and sample.speed_kmh < 1.2
            ]
            self.assertGreater(len(held), 2)
        finally:
            service.close()


if __name__ == '__main__':
    unittest.main()
