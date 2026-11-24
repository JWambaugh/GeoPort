"""GPS wander stays smooth, and the simulated fix chains from stop to stop."""
from __future__ import annotations

import math
import os
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from geoport.services.accuracy import GpsAccuracyModel  # noqa: E402
from geoport.services.control import api_access_allowed, resolve_endpoints  # noqa: E402
from geoport.services.drive import build_profile, haversine_m, iter_samples  # noqa: E402
from geoport.services.position import PositionService  # noqa: E402
from tests.test_drive import _straight  # noqa: E402


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy)


class AccuracyModelTests(unittest.TestCase):
    def test_zero_radius_is_exact(self) -> None:
        model = GpsAccuracyModel(accuracy_m=0, rng=random.Random(1))
        model.north_m = 40
        self.assertEqual(model.step(1.0), (0.0, 0.0))
        model.set_meters(25)
        model.step(0.5)
        model.set_meters(0)
        self.assertEqual(model.offset(), (0.0, 0.0))

    def test_wander_is_correlated_not_white_noise(self) -> None:
        model = GpsAccuracyModel(accuracy_m=30, rng=random.Random(2))
        north = []
        radius = []
        steps = []
        prev = 0.0
        for _ in range(80):
            model.step(0.5)
        for _ in range(500):
            n, e = model.step(0.25)
            north.append(n)
            radius.append(math.hypot(n, e))
            steps.append(abs(n - prev))
            prev = n
        self.assertGreater(_pearson(north[:-1], north[1:]), 0.8)
        self.assertLess(sum(steps) / len(steps), 8.0)
        rms = math.sqrt(sum(r * r for r in radius) / len(radius))
        self.assertGreater(rms, 12)
        self.assertLess(rms, 50)

    def test_slider_is_clamped(self) -> None:
        model = GpsAccuracyModel()
        self.assertEqual(model.set_meters(250), 100)
        self.assertEqual(model.set_meters(-4), 0)


class PositionChainTests(unittest.TestCase):
    def test_position_survives_restart_with_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'simulated_position.json'
            first = PositionService(path=path, rng=random.Random(1))
            fix = first.note(-27.47, 153.02, source='teleport', device={'udid': 'ABC', 'name': 'Jordan iPhone'})
            self.assertAlmostEqual(fix['lat'], -27.47, places=5)
            second = PositionService(path=path, rng=random.Random(1))
            current = second.current()
            self.assertIsNotNone(current)
            assert current is not None
            self.assertAlmostEqual(current[0], -27.47, places=5)
            self.assertEqual(second.snapshot()['udid'], 'ABC')
            self.assertEqual(second.snapshot()['device_name'], 'Jordan iPhone')

    def test_next_drive_starts_where_the_previous_one_stopped(self) -> None:
        plan = build_profile(_straight(70, 40, 800, 800), 0)
        samples = [sample for sample in iter_samples(plan, 0.5) if not sample.finished]
        with tempfile.TemporaryDirectory() as tmp:
            position = PositionService(path=Path(tmp) / 'pos.json', rng=random.Random(0))
            cut = samples[len(samples) // 3]
            for sample in samples:
                position.note(sample.lat, sample.lng, source='drive', step=False)
                if sample.t >= cut.t:
                    break
            start, end, _label = resolve_endpoints(
                {'to': {'lat': 1.0, 'lng': 1.0}},
                position,
                geocoder=lambda _q: [],
            )
            self.assertLess(haversine_m(start[0], start[1], cut.lat, cut.lng), 1.0)
            self.assertEqual(end, (1.0, 1.0))
            # Completing the rest of the drive moves the fix to the arrival point.
            for sample in samples:
                position.note(sample.lat, sample.lng, source='drive', step=False)
            arrival = samples[-1]
            start2, _end2, _label2 = resolve_endpoints(
                {'to': {'address': 'ignored', 'lat': 2.0, 'lng': 2.0}},
                position,
                geocoder=lambda _q: [],
            )
            self.assertLess(haversine_m(start2[0], start2[1], arrival.lat, arrival.lng), 1.0)

    def test_explicit_from_overrides_current_and_missing_current_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            position = PositionService(path=Path(tmp) / 'pos.json')
            with self.assertRaises(Exception) as raised:
                resolve_endpoints({'to': {'lat': 3, 'lng': 3}}, position, lambda _q: [])
            self.assertIn('No simulated location', str(raised.exception))
            position.note(5, 6, source='set', step=False)
            start, _end, _label = resolve_endpoints(
                {'to': {'lat': 7, 'lng': 8}, 'from': {'lat': 9, 'lng': 10}},
                position,
                lambda _q: [],
            )
            self.assertEqual(start, (9.0, 10.0))

    def test_api_is_loopback_unless_overridden(self) -> None:
        previous = os.environ.pop('GEOPORT_API_ALLOW_REMOTE', None)
        try:
            self.assertTrue(api_access_allowed('127.0.0.1'))
            self.assertFalse(api_access_allowed('192.168.1.20'))
            os.environ['GEOPORT_API_ALLOW_REMOTE'] = '1'
            self.assertTrue(api_access_allowed('192.168.1.20'))
        finally:
            if previous is None:
                os.environ.pop('GEOPORT_API_ALLOW_REMOTE', None)
            else:
                os.environ['GEOPORT_API_ALLOW_REMOTE'] = previous


class AutomationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls._previous_state = os.environ.get('GEOPORT_STATE_DIR')
        cls._previous_auto = os.environ.get('GEOPORT_AUTOCONNECT')
        os.environ['GEOPORT_STATE_DIR'] = cls._tmp.name
        os.environ['GEOPORT_AUTOCONNECT'] = '0'
        from geoport.runtime import get_runtime
        from geoport.web.app import create_app
        cls.runtime = get_runtime()
        cls.runtime.start()
        cls.app, _socket = create_app(runtime=cls.runtime)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        position = cls.app.extensions['geoport'].get('position')
        drive = cls.app.extensions['geoport'].get('drive')
        if position is not None:
            position.stop()
        if drive is not None:
            drive.close()
        if cls._previous_state is None:
            os.environ.pop('GEOPORT_STATE_DIR', None)
        else:
            os.environ['GEOPORT_STATE_DIR'] = cls._previous_state
        if cls._previous_auto is None:
            os.environ.pop('GEOPORT_AUTOCONNECT', None)
        else:
            os.environ['GEOPORT_AUTOCONNECT'] = cls._previous_auto
        cls._tmp.cleanup()

    def test_teleport_status_accuracy_and_clear(self) -> None:
        denied = self.client.post(
            '/api/v1/teleport',
            json={'lat': 1, 'lng': 2},
            environ_overrides={'REMOTE_ADDR': '8.8.8.8'},
        )
        self.assertEqual(denied.status_code, 403)
        moved = self.client.post('/api/v1/teleport', json={'lat': -27.47, 'lng': 153.026})
        self.assertEqual(moved.status_code, 200, moved.get_json())
        body = moved.get_json()
        self.assertAlmostEqual(body['position']['lat'], -27.47, places=4)
        status = self.client.get('/api/v1/status').get_json()
        self.assertAlmostEqual(status['position']['lat'], body['position']['lat'], places=5)
        page = self.client.get('/api/status').get_json()
        self.assertAlmostEqual(page['position']['lat'], status['position']['lat'], places=5)
        tuned = self.client.post('/api/accuracy', json={'meters': 40})
        self.assertEqual(tuned.status_code, 200)
        self.assertEqual(tuned.get_json()['accuracy_m'], 40)
        cleared = self.client.post('/api/v1/clear')
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(cleared.get_json()['position'])

    def test_chained_drive_plans_use_the_latest_fix(self) -> None:
        import geoport.services.drive as drive_mod
        import geoport.services.stops as stops_mod
        calls = []
        original = drive_mod.fetch_segments
        original_controls = drive_mod._controls_for
        original_lookup = stops_mod.StopLookup

        class ImmediateLookup:
            def __init__(self, *args, **kwargs):
                pass
            def start(self, on_update=None):
                return None
            def wait(self, budget_s):
                return [], {'state': 'complete', 'chunks': [], 'note': None}
            def cancel(self):
                return None

        def fake(start, end, provider, google_api_key, http, anchor_end=True):
            calls.append((start, end))
            segments = drive_mod.segments_from_points(
                [start, end],
                [50],
                sources=['simulate'],
            )
            return segments, 'osrm'

        def no_stops(segments, http, include_stops, stops_seed):
            return [], {
                'stop_signs': 0, 'signals': 0, 'available': True, 'note': None,
                'expected_signal_s': 0, 'realized_signal_s': 0,
            }

        drive_mod.fetch_segments = fake
        drive_mod._controls_for = no_stops
        stops_mod.StopLookup = ImmediateLookup
        try:
            self.client.post('/api/accuracy', json={'meters': 0})
            first = self.client.post('/api/v1/teleport', json={'lat': -27.5, 'lng': 153.0})
            self.assertEqual(first.status_code, 200, first.get_json())
            planned = self.client.post('/api/v1/drive', json={
                'to': {'lat': -27.51, 'lng': 153.02},
                'start': False,
            })
            self.assertEqual(planned.status_code, 200, planned.get_json())
            self.assertAlmostEqual(calls[-1][0][0], -27.5, places=3)
            # The drive's last published fix is the arrival point.
            position = self.app.extensions['geoport']['position']
            position.note(-27.51, 153.02, source='drive', step=False)
            second = self.client.post('/api/v1/drive', json={
                'to': {'lat': -27.52, 'lng': 153.04},
                'start': False,
            })
            self.assertEqual(second.status_code, 200, second.get_json())
            self.assertAlmostEqual(calls[-1][0][0], -27.51, places=3)
            self.assertAlmostEqual(calls[-1][0][1], 153.02, places=3)
            self.assertNotAlmostEqual(calls[-1][0][0], -27.5, places=3)
        finally:
            drive_mod.fetch_segments = original
            drive_mod._controls_for = original_controls
            stops_mod.StopLookup = original_lookup

    def test_connect_endpoint_runs_the_watchdog_hook(self) -> None:
        called = []
        previous = self.app.extensions['geoport'].get('connect_now')
        self.app.extensions['geoport']['connect_now'] = lambda force=False: called.append(force)
        try:
            response = self.client.post('/api/v1/connect')
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(called, [True])
            self.assertIn('reconnecting', response.get_json())
            self.assertIn('reconnect_attempts', response.get_json())
        finally:
            self.app.extensions['geoport']['connect_now'] = previous

    def test_devices_route_uses_the_scanner(self) -> None:
        scanner = self.app.extensions['geoport']['scanner']
        previous = scanner.discover_devices

        async def fake_discover(active_session_udid=None):
            return {'udid-1': {'USB': [{'DeviceName': 'Phone'}]}}

        scanner.discover_devices = fake_discover
        try:
            response = self.client.get('/api/v1/devices')
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertIn('udid-1', response.get_json()['devices'])
        finally:
            scanner.discover_devices = previous


if __name__ == '__main__':
    unittest.main()
