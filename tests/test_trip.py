"""Multi-stop trips. Routing is mocked; the drive player is not asked to sleep."""
from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from geoport.services.drive import (  # noqa: E402
    EARTH_RADIUS_M,
    build_profile,
    haversine_m,
    iter_samples,
    segments_from_points,
)
from geoport.services.position import PositionService  # noqa: E402
from geoport.services.trip import (  # noqa: E402
    WALK_MPS,
    build_walk_plan as walk_plan,
    fetch_walk_points,
    reconnect_drive_state,
)
from geoport.services.trip import TripService, register_trip_routes  # noqa: E402


def _move(lat: float, lng: float, north_m: float = 0.0, east_m: float = 0.0) -> tuple[float, float]:
    lat2 = lat + (north_m / EARTH_RADIUS_M) * (180.0 / math.pi)
    cos = max(0.2, math.cos(math.radians(lat)))
    lng2 = lng + (east_m / (EARTH_RADIUS_M * cos)) * (180.0 / math.pi)
    return lat2, lng2


def _short(start: tuple[float, float], end: tuple[float, float], meters: float) -> tuple[float, float]:
    dist = haversine_m(start[0], start[1], end[0], end[1])
    if dist <= meters + 1:
        return end
    frac = (dist - meters) / dist
    return (
        start[0] + (end[0] - start[0]) * frac,
        start[1] + (end[1] - start[1]) * frac,
    )


class FlipRng:
    def __init__(self) -> None:
        self.n = 0

    def random(self) -> float:
        return 0.0

    def uniform(self, low: float, high: float) -> float:
        self.n += 1
        return low if self.n % 2 else high


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')

    def json(self) -> dict:
        return self._payload


class FakeHttp:
    def __init__(self, get=None, post=None) -> None:
        self._get = get
        self._post = post
        self.gets: list[str] = []
        self.posts: list[str] = []

    def get(self, url, **kwargs):
        self.gets.append(url)
        if self._get is None:
            raise RuntimeError('no get')
        return self._get(url, **kwargs)

    def post(self, url, **kwargs):
        self.posts.append(url)
        if self._post is None:
            raise RuntimeError('no post')
        return self._post(url, **kwargs)


class FakeDrive:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.state = 'idle'
        self._plan = None
        self._user = False

    def plan(self, start, end, offset_pct=0, provider='osrm', google_api_key='', include_stops=True, stops_seed=None, anchor_end=True):
        self.calls.append(('plan', bool(include_stops), bool(anchor_end)))
        target = end if anchor_end else _short(start, end, 30)
        segments = segments_from_points([start, target], [36])
        plan = build_profile(segments, offset_pct, provider='osrm')
        self._plan = plan
        self.state = 'idle'
        return plan

    def start(self, offset_pct=None):
        self.state = 'playing'
        self.calls.append(('start',))
        return self.status()

    def play_plan(self, plan):
        self._plan = plan
        self.state = 'playing'
        self.calls.append(('play', plan.provider, round(plan.distance_m, 1), plan.stops_summary.get('stop_signs')))
        return self.status()

    def pause(self):
        self.state = 'paused'
        return self.status()

    def resume(self):
        self.state = 'playing'
        return self.status()

    def stop(self, user=False):
        self._user = bool(user)
        self.state = 'idle'
        return self.status()

    def consume_user_stop(self) -> bool:
        flag = self._user
        self._user = False
        return flag

    def status(self) -> dict:
        remaining = 0.0
        if self._plan is not None and self.state == 'playing':
            remaining = self._plan.duration_s
        return {'ok': True, 'state': self.state, 'remaining_s': remaining}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _osrm_leg(coords):
    return {
        'code': 'Ok',
        'routes': [{
            'geometry': {'coordinates': [[lng, lat] for lat, lng in coords]},
            'legs': [{'annotation': {
                'distance': [100.0] * (len(coords) - 1),
                'speed': [1.3] * (len(coords) - 1),
            }}],
        }],
    }


class WalkGeometryTests(unittest.TestCase):
    def test_pace_is_about_1_3_meters_per_second(self) -> None:
        start = (39.0, -121.0)
        end = _move(*start, north_m=200)
        plan = walk_plan([start, end], WALK_MPS, variation=0, allow_pause=False)
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan.duration_s, 200 / WALK_MPS, delta=0.05)
        self.assertEqual(plan.stops_summary['stop_signs'], 0)
        self.assertEqual(plan.stops_summary['signals'], 0)
        self.assertIn('stop signs', plan.stops_summary['note'])
        samples = [sample for sample in iter_samples(plan, 0.5) if not sample.finished and sample.speed_mps > 0.2]
        self.assertGreater(len(samples), 20)
        self.assertAlmostEqual(sum(sample.speed_mps for sample in samples) / len(samples), WALK_MPS, delta=0.05)

    def test_speed_varies_and_a_long_walk_can_pause(self) -> None:
        start = (39.0, -121.0)
        mid = _move(*start, north_m=80)
        end = _move(*mid, north_m=80)
        plan = walk_plan([start, mid, end], WALK_MPS, rng=FlipRng(), variation=0.08, allow_pause=True)
        moving = [phase.v0 for phase in plan.phases if phase.v0 > 0.2 and phase.d1 > phase.d0]
        self.assertGreater(len(set(round(speed, 3) for speed in moving)), 1)
        self.assertTrue(all(WALK_MPS * 0.9 <= speed <= WALK_MPS * 1.1 for speed in moving))
        pauses = [phase for phase in plan.phases if phase.v0 == 0 and phase.d0 == phase.d1 and phase.t1 > phase.t0]
        self.assertEqual(len(pauses), 1)
        self.assertGreater(pauses[0].t1 - pauses[0].t0, 0.5)
        self.assertLess(pauses[0].t1 - pauses[0].t0, 2.5)
        self.assertGreater(plan.duration_s, plan.distance_m / WALK_MPS)

    def test_short_walk_has_no_pause(self) -> None:
        start = (39.0, -121.0)
        end = _move(*start, east_m=20)
        plan = walk_plan([start, end], WALK_MPS, rng=FlipRng(), variation=0, allow_pause=True)
        pauses = [phase for phase in plan.phases if phase.v0 == 0 and phase.d0 == phase.d1]
        self.assertEqual(pauses, [])

    def test_osrm_foot_geometry_is_used(self) -> None:
        start = (39.0, -121.0)
        bend = _move(*start, north_m=40, east_m=30)
        end = _move(*start, north_m=80)
        http = FakeHttp(get=lambda url, **kwargs: FakeResponse(_osrm_leg([start, bend, end])))
        points, provider = fetch_walk_points(start, end, http)
        self.assertEqual(provider, 'osrm-foot')
        self.assertIn('foot', http.gets[0])
        self.assertEqual(http.posts, [])
        self.assertEqual(points[1], bend)

    def test_valhalla_pedestrian_is_the_fallback(self) -> None:
        start = (39.0, -121.0)
        end = _move(*start, north_m=60)
        shape = {
            'trip': {'legs': [{'shape': {'type': 'LineString', 'coordinates': [
                [start[1], start[0]],
                [start[1] + 0.001, start[0] + 0.0004],
                [end[1], end[0]],
            ]}}]},
        }

        def fail_get(url, **kwargs):
            raise TimeoutError('osrm foot down')

        http = FakeHttp(get=fail_get, post=lambda url, **kwargs: FakeResponse(shape))
        points, provider = fetch_walk_points(start, end, http)
        self.assertEqual(provider, 'valhalla-pedestrian')
        self.assertEqual(len(http.gets), 2)
        self.assertEqual(len(points), 3)
        self.assertGreater(points[1][1], start[1])

    def test_straight_line_when_routers_fail(self) -> None:
        start = (39.0, -121.0)
        end = _move(*start, north_m=50)

        def boom(*args, **kwargs):
            raise TimeoutError('down')

        http = FakeHttp(get=boom, post=boom)
        points, provider = fetch_walk_points(start, end, http)
        self.assertEqual(provider, 'straight')
        self.assertEqual(points[0], start)
        self.assertEqual(points[-1], end)
        self.assertGreater(len(points), 2)
        self.assertNotIn('overpass', ' '.join(http.gets + http.posts))


class TripPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.drive_calls: list[tuple] = []
        self.walk_calls: list[tuple] = []
        self.position = PositionService(path=Path(self.tmp.name) / 'pos.json')
        self.origin = (39.20, -121.06)
        self.position.note(self.origin[0], self.origin[1], source='set')
        self.target = _move(*self.origin, north_m=800)
        self.pin = _move(*self.target, east_m=40)
        self.pin2 = _move(*self.pin, east_m=30)
        self.costco = _move(*self.target, north_m=1600)
        self.drive = FakeDrive()
        self.trip = TripService(
            self.drive,
            position=self.position,
            path=Path(self.tmp.name) / 'trip.json',
            library_path=Path(self.tmp.name) / 'trips.json',
            drive_router=self._drive_router,
            walk_router=self._walk_router,
            now=self.clock,
            manual=True,
        )

    def tearDown(self) -> None:
        self.trip.close()
        self.position.stop()
        self.tmp.cleanup()

    def _drive_router(self, start, end, include_stops, offset):
        self.drive_calls.append((include_stops, offset, start, end))
        road = _short(start, end, 30)
        segments = segments_from_points([start, road], [40])
        summary = {
            'stop_signs': 1 if include_stops else 0,
            'signals': 2 if include_stops else 0,
            'available': True,
            'note': None if include_stops else 'Stops disabled',
            'expected_signal_s': 11.25 if include_stops else 0,
            'realized_signal_s': 0,
        }
        plan = build_profile(segments, offset, provider='osrm', stops_summary=summary)
        return plan, road

    def _walk_router(self, start, end):
        self.walk_calls.append((start, end))
        mid = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0 + 0.0002)
        return [start, mid, end], 'osrm-foot'

    def _body(self, start=False, inside=True, signs=True):
        return {
            'from': {'lat': self.origin[0], 'lng': self.origin[1]},
            'start': start,
            'include_stops': signs,
            'walk_speed_mps': WALK_MPS,
            'walk_variation': 0,
            'walk_seed': 2,
            'stops': [
                {
                    'lat': self.target[0], 'lng': self.target[1], 'label': 'Target',
                    'dwell_minutes': 20, 'mode': 'drive', 'go_inside': inside,
                },
                {
                    'lat': self.pin[0], 'lng': self.pin[1], 'label': 'Lot pin',
                    'dwell_minutes': 5, 'mode': 'walk',
                },
                {
                    'lat': self.pin2[0], 'lng': self.pin2[1], 'label': 'Gate',
                    'dwell_minutes': 0, 'mode': 'walk', 'waypoint': True,
                },
                {
                    'lat': self.costco[0], 'lng': self.costco[1], 'label': 'Costco',
                    'dwell_minutes': 40, 'mode': 'drive', 'go_inside': False,
                },
            ],
        }

    def test_plan_includes_walk_inside_walk_legs_and_dwells(self) -> None:
        status = self.trip.plan_body(self._body())
        stops = status['stops']
        self.assertEqual([stop['label'] for stop in stops], ['Target', 'Lot pin', 'Gate', 'Costco'])
        target, lot, gate, costco = stops
        self.assertAlmostEqual(target['walk_in_s'], 30 / WALK_MPS, delta=1.5)
        self.assertEqual(target['dwell_s'], 1200)
        self.assertEqual(target['stop_signs'], 1)
        self.assertEqual(target['signals'], 2)
        self.assertGreater(target['drive_s'], 30)
        self.assertTrue(any(part['role'] == 'walk_in' and part['mode'] == 'walk' for part in target['parts']))
        self.assertEqual(lot['mode'], 'walk')
        self.assertEqual(lot['stop_signs'], 0)
        self.assertEqual(lot['signals'], 0)
        self.assertGreater(lot['walk_s'], 40 / WALK_MPS)
        self.assertEqual(lot['parts'][0]['provider'], 'osrm-foot')
        self.assertGreaterEqual(len(lot['parts'][0]['geometry']), 3)
        self.assertTrue(gate['waypoint'])
        self.assertEqual(gate['dwell_s'], 0)
        self.assertEqual(gate['walk_in_s'], 0)
        self.assertEqual(costco['walk_in_s'], 0)
        self.assertGreater(costco['walk_back_s'], 20 / WALK_MPS)
        self.assertEqual(costco['dwell_s'], 2400)
        self.assertIn('T', target['arrive_at'])
        self.assertIn('T', target['depart_at'])
        dwell = 20 * 60 + 5 * 60 + 40 * 60
        self.assertGreater(status['total_duration_s'], dwell + target['walk_in_s'] + lot['walk_s'])
        self.assertEqual(len(self.drive_calls), 2)
        self.assertTrue(all(call[0] for call in self.drive_calls))
        self.assertEqual(len(self.walk_calls), 2)
        self.assertEqual(self.drive.calls, [])

    def test_park_only_does_not_walk_inside(self) -> None:
        status = self.trip.plan_body(self._body(inside=False))
        self.assertEqual(status['stops'][0]['walk_in_s'], 0)
        self.assertEqual(status['stops'][0]['go_inside'], False)
        self.assertFalse(any(part['role'] == 'walk_in' for part in status['stops'][0]['parts']))

    def test_signs_can_be_turned_off_for_drives_only(self) -> None:
        status = self.trip.plan_body(self._body(signs=False))
        self.assertEqual(status['stops'][0]['stop_signs'], 0)
        self.assertEqual(status['stops'][0]['signals'], 0)
        self.assertTrue(all(call[0] is False for call in self.drive_calls))
        self.assertGreater(status['stops'][1]['walk_s'], 0)

    def test_playback_walks_inside_then_dwells_and_pause_freezes_the_clock(self) -> None:
        self.trip.plan_body(self._body(start=True))
        self.assertEqual(self.trip.phase, 'drive')
        self.assertEqual(self.drive.calls[0][:3], ('plan', True, False))
        self.trip.note_drive_state('finished')
        self.assertEqual(self.trip.phase, 'walk_in')
        self.assertEqual(self.drive.calls[-1][0], 'play')
        self.assertEqual(self.drive.calls[-1][3], 0)
        self.trip.note_drive_state('finished')
        self.assertEqual(self.trip.phase, 'dwell')
        self.assertIn('Dwelling at Target', self.trip.status()['detail'])
        self.assertEqual(self.position.snapshot()['accuracy_m'], 12)
        self.clock.t += 15
        self.trip.pause()
        self.assertEqual(self.trip.state, 'paused')
        self.assertIn('Paused', self.trip.status()['detail'])
        paused_left = self.trip.status()['dwell_remaining_s']
        self.clock.t += 500
        self.trip.tick()
        self.assertEqual(self.trip.phase, 'dwell')
        self.assertAlmostEqual(self.trip.status()['dwell_remaining_s'], paused_left, delta=0.2)
        self.trip.resume()
        self.clock.t += paused_left + 1
        self.trip.tick()
        self.assertEqual(self.trip.phase, 'walk')
        self.assertEqual(self.position.snapshot()['accuracy_m'], 0)
        self.assertEqual(self.trip.status()['current_label'], 'Lot pin')
        self.assertEqual(self.trip.status()['next_label'], 'Gate')

    def test_skip_drive_goes_to_the_walk_in(self) -> None:
        self.trip.plan_body(self._body(start=True))
        self.trip.skip()
        self.assertEqual(self.trip.phase, 'walk_in')
        self.assertGreater(haversine_m(self.position.true_lat, self.position.true_lng, self.target[0], self.target[1]), 20)

    def test_edit_dwell_while_dwelling(self) -> None:
        self.trip.plan_body(self._body(start=True))
        self.trip.note_drive_state('finished')
        self.trip.note_drive_state('finished')
        self.assertEqual(self.trip.phase, 'dwell')
        edited = self.trip.status()['stops']
        edited[0]['dwell_minutes'] = 30
        self.trip.replace_stops(edited)
        self.assertEqual(self.trip.phase, 'dwell')
        self.assertGreater(self.trip.status()['dwell_remaining_s'], 1700)
        self.clock.t += 30
        self.trip.tick()
        self.assertEqual(self.trip.phase, 'dwell')

    def test_remove_a_future_stop_while_running(self) -> None:
        self.trip.plan_body(self._body(start=True))
        self.trip.note_drive_state('finished')
        self.trip.note_drive_state('finished')
        edited = [stop for stop in self.trip.status()['stops'] if stop['label'] != 'Gate']
        status = self.trip.replace_stops(edited)
        self.assertEqual([stop['label'] for stop in status['stops']], ['Target', 'Lot pin', 'Costco'])
        self.assertEqual(status['next_label'], 'Lot pin')

    def test_state_survives_restart_from_the_current_position(self) -> None:
        self.trip.plan_body(self._body(start=True))
        self.trip.note_drive_state('finished')
        self.trip.note_drive_state('finished')
        self.clock.t += 12
        self.trip.tick()
        self.trip.close()
        restored = TripService(
            FakeDrive(),
            position=self.position,
            path=Path(self.tmp.name) / 'trip.json',
            library_path=Path(self.tmp.name) / 'trips.json',
            drive_router=self._drive_router,
            walk_router=self._walk_router,
            now=self.clock,
            manual=True,
        )
        restored.maybe_resume()
        self.assertEqual(restored.state, 'playing')
        self.assertEqual(restored.phase, 'dwell')
        self.assertEqual(restored.status()['current_label'], 'Target')
        self.assertAlmostEqual(restored.status()['dwell_remaining_s'], 1200 - 12, delta=1.5)
        restored.close()

    def test_preview_reuses_unchanged_legs_and_skips_signs(self) -> None:
        body = self._body()
        body['start'] = False
        first = self.trip.preview_body(body)
        self.assertEqual(first['stop_count'], 4)
        self.assertTrue(all(call[0] is False for call in self.drive_calls))
        for stop, raw in zip(first['stops'], body['stops']):
            raw['id'] = stop['id']
        drives = len(self.drive_calls)
        walks = len(self.walk_calls)
        self.trip.preview_body(body)
        self.assertEqual(len(self.drive_calls), drives)
        self.assertEqual(len(self.walk_calls), walks)
        self.assertGreater(self.trip._cache_hits, 0)
        body['stops'][0]['dwell_minutes'] = 25
        self.trip.preview_body(body)
        self.assertEqual(len(self.drive_calls), drives)
        self.assertEqual(self.trip.stops[0].dwell_minutes, 25)
        body['stops'][1]['mode'] = 'drive'
        body['stops'][1]['go_inside'] = False
        self.trip.preview_body(body)
        self.assertGreater(len(self.drive_calls), drives)
        self.assertEqual(self.trip.stops[0].id, first['stops'][0]['id'])

    def test_edit_rejects_a_finished_stop_and_keeps_its_id(self) -> None:
        status = self.trip.plan_body(self._body(start=True))
        self.trip.index = 1
        self.trip.stops[0].arrived_at = '2026-01-01T00:00:00+00:00'
        self.trip.stops[0].departed_at = self.trip.stops[0].arrived_at
        with self.assertRaises(Exception) as raised:
            self.trip.update_stop('1', {'dwell_minutes': 5})
        self.assertIn('finished', str(raised.exception).lower())
        edited = self.trip.update_stop(status['stops'][1]['id'], {'label': 'Across the lot', 'dwell_minutes': 8})
        self.assertEqual(edited['stops'][1]['label'], 'Across the lot')
        self.assertEqual(edited['stops'][1]['id'], status['stops'][1]['id'])
        self.assertEqual(edited['stops'][1]['dwell_minutes'], 8)

    def test_replan_without_a_fix_keeps_the_previous_start(self) -> None:
        self.trip.plan_body(self._body())
        self.position.clear()
        body = self._body()
        del body['from']
        status = self.trip.plan_body(body)
        self.assertEqual(status['stop_count'], 4)
        self.assertEqual(status['stops'][0]['label'], 'Target')
        self.assertGreater(status['total_distance_m'], 1000)

    def test_saved_named_trip_reloads(self) -> None:
        self.trip.plan_body(self._body())
        saved = self.trip.save_named('errands')
        self.assertEqual(saved['stops'], 4)
        self.trip.stop()
        self.trip.stops = []
        loaded = self.trip.load_named('errands', start=False)
        self.assertEqual(loaded['loaded'], 'errands')
        self.assertEqual(loaded['stop_count'], 4)
        self.assertEqual(loaded['stops'][0]['label'], 'Target')

    def test_reconnect_stays_armed_during_a_dwell(self) -> None:
        self.assertEqual(reconnect_drive_state('finished', 'playing'), 'playing')
        self.assertEqual(reconnect_drive_state('idle', 'paused'), 'paused')
        self.assertEqual(reconnect_drive_state('playing', 'idle'), 'playing')


class TripApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.position = PositionService(path=Path(self.tmp.name) / 'pos.json')
        self.origin = (39.2, -121.06)
        self.position.note(*self.origin, source='set')
        self.drive_calls: list[bool] = []

        def drive_router(start, end, include_stops, offset):
            self.drive_calls.append(include_stops)
            road = _short(start, end, 25)
            plan = build_profile(segments_from_points([start, road], [40]), 0, provider='osrm')
            return plan, road

        def walk_router(start, end):
            return [start, end], 'straight'

        def geocoder(query: str):
            if 'Target' in query:
                point = _move(*self.origin, north_m=500)
                return [{'lat': point[0], 'lng': point[1], 'display_name': 'Target, Grass Valley'}]
            raise RuntimeError(query)

        self.trip = TripService(
            FakeDrive(),
            position=self.position,
            path=Path(self.tmp.name) / 'trip.json',
            library_path=Path(self.tmp.name) / 'trips.json',
            drive_router=drive_router,
            walk_router=walk_router,
            geocoder=geocoder,
            now=Clock(),
            manual=True,
        )
        from flask import Flask
        self.app = Flask(__name__)
        self.app.extensions['geoport'] = {}
        register_trip_routes(self.app, self.trip)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.trip.close()
        self.position.stop()
        self.tmp.cleanup()

    def test_create_status_edit_and_remote_is_rejected(self) -> None:
        addressed = self.client.post('/api/v1/trip', json={
            'start': False,
            'walk_variation': 0,
            'stops': [
                {'address': 'Target, Grass Valley', 'dwell_minutes': 20, 'mode': 'drive', 'label': 'Target'},
            ],
        })
        self.assertEqual(addressed.status_code, 200)
        self.assertEqual(addressed.get_json()['stops'][0]['label'], 'Target')
        self.assertGreater(addressed.get_json()['stops'][0]['walk_in_s'], 0)

        target = _move(*self.origin, north_m=500)
        created = self.client.post('/api/v1/trip', json={
            'start': False,
            'walk_variation': 0,
            'stops': [
                {'lat': target[0], 'lng': target[1], 'label': 'Target', 'dwell_minutes': 20, 'mode': 'drive'},
                {'lat': target[0], 'lng': target[1] + 0.001, 'label': 'Pin', 'dwell_minutes': 5, 'mode': 'walk'},
                {'lat': target[0], 'lng': target[1] + 0.002, 'dwell_minutes': 0, 'mode': 'walk', 'waypoint': True},
            ],
        })
        self.assertEqual(created.status_code, 200)
        body = created.get_json()
        self.assertEqual(body['stop_count'], 3)
        self.assertGreater(body['stops'][0]['walk_in_s'], 0)
        self.assertEqual(body['stops'][1]['mode'], 'walk')
        self.assertTrue(body['stops'][2]['waypoint'])
        self.assertEqual(body['state'], 'idle')

        status = self.client.get('/api/v1/trip')
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()['stops'][0]['label'], 'Target')

        edited = self.client.post('/api/trip/stops', json={
            'stops': [
                {'lat': target[0], 'lng': target[1], 'label': 'Target', 'dwell_minutes': 15, 'mode': 'drive', 'id': body['stops'][0]['id']},
                {'lat': target[0], 'lng': target[1] + 0.001, 'label': 'Pin', 'dwell_minutes': 8, 'mode': 'walk'},
            ],
        })
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.get_json()['stop_count'], 2)
        self.assertEqual(edited.get_json()['stops'][0]['dwell_minutes'], 15)

        target = _move(*self.origin, north_m=500)
        preview = self.client.post('/api/trip/preview', json={
            'start': False,
            'walk_variation': 0,
            'preview_id': 'p1',
            'stops': [
                {'id': 'aaa', 'lat': target[0], 'lng': target[1], 'label': 'Target', 'dwell_minutes': 20, 'mode': 'drive'},
                {'id': 'bbb', 'lat': target[0], 'lng': target[1] + 0.001, 'label': 'Pin', 'dwell_minutes': 5, 'mode': 'walk'},
                {'id': 'ccc', 'lat': target[0] + 0.004, 'lng': target[1] + 0.002, 'label': 'Next', 'dwell_minutes': 10, 'mode': 'drive', 'go_inside': False},
            ],
        })
        self.assertEqual(preview.status_code, 200, preview.get_json())
        preview_body = preview.get_json()
        self.assertTrue(preview_body.get('preview'))
        self.assertEqual([stop['id'] for stop in preview_body['stops']], ['aaa', 'bbb', 'ccc'])
        self.assertGreater(preview_body['stops'][0]['distance_m'], 0)
        preview_calls = [flag for flag in self.drive_calls]
        self.assertTrue(preview_calls)
        self.assertFalse(preview_calls[-1])
        calls_before = len(self.drive_calls)
        again = self.client.post('/api/trip/preview', json={
            'preview_id': 'p2',
            'stops': preview_body['stops'],
        })
        self.assertEqual(again.status_code, 200, again.get_json())
        self.assertEqual(len(self.drive_calls), calls_before)
        edited = self.client.patch('/api/v1/trip/stops/bbb', json={'dwell_minutes': 30, 'label': 'Side door'})
        self.assertEqual(edited.status_code, 200, edited.get_json())
        self.assertEqual(edited.get_json()['stops'][1]['label'], 'Side door')
        self.assertEqual(edited.get_json()['stops'][1]['dwell_minutes'], 30)
        self.assertEqual(edited.get_json()['stops'][1]['id'], 'bbb')
        self.assertEqual(len(self.drive_calls), calls_before)
        moved = self.client.patch('/api/v1/trip/stops/2', json={
            'mode': 'drive',
            'go_inside': False,
            'dwell_minutes': 4,
        })
        self.assertEqual(moved.status_code, 200, moved.get_json())
        self.assertEqual(moved.get_json()['stops'][1]['mode'], 'drive')
        self.assertGreater(len(self.drive_calls), calls_before)
        denied = self.client.get('/api/v1/trip', environ_base={'REMOTE_ADDR': '10.1.1.5'})
        self.assertEqual(denied.status_code, 403)
        allowed = self.client.get('/api/trip', environ_base={'REMOTE_ADDR': '10.1.1.5'})
        self.assertEqual(allowed.status_code, 200)


if __name__ == '__main__':
    unittest.main()
