"""geoportctl argument passing. A local HTTP stub stands in for GeoPort."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CTL = ROOT / 'scripts' / 'geoportctl'


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self._take()

    def do_PATCH(self) -> None:
        self._take()

    def _take(self) -> None:
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode() or '{}')
        self.server.hits.append((self.command, self.path, payload))  # type: ignore[attr-defined]
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


class GeoportctlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.hits = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.env = os.environ.copy()
        self.env['GEOPORT_URL'] = f'http://{host}:{port}'

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def run_ctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ['bash', str(CTL), *args],
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def test_save_and_load_pass_the_name(self) -> None:
        saved = self.run_ctl('trip', 'save', 'Grocery run')
        self.assertEqual(saved.returncode, 0, saved.stderr)
        loaded = self.run_ctl('trip', 'load', "O'Brien", '--start')
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        self.assertEqual(self.server.hits[0], (
            'POST', '/api/v1/trip/library', {'name': 'Grocery run'},
        ))
        self.assertEqual(self.server.hits[1], (
            'POST', '/api/v1/trip/library/load', {'name': "O'Brien", 'start': True},
        ))

    def test_create_flags_for_straight_walk_and_wander(self) -> None:
        result = self.run_ctl(
            'trip', '--from', '1,2', '--no-start',
            '--walk-direct', '3,4', '5',
            '--wander', '0.6', '--radius', '25',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        method, path, payload = self.server.hits[-1]
        self.assertEqual((method, path), ('POST', '/api/v1/trip'))
        self.assertFalse(payload['start'])
        stop = payload['stops'][0]
        self.assertEqual(stop['mode'], 'walk')
        self.assertTrue(stop['direct'])
        self.assertEqual(stop['route'], 'straight')
        self.assertEqual(stop['dwell_mode'], 'wander')
        self.assertEqual(stop['wander_mps'], 0.6)
        self.assertEqual(stop['wander_radius_m'], 25)
        self.assertEqual(stop['lat'], 3)
        self.assertEqual(stop['lng'], 4)

    def test_edit_still_and_wander(self) -> None:
        still = self.run_ctl('trip', 'edit', '2', '--still')
        self.assertEqual(still.returncode, 0, still.stderr)
        wandered = self.run_ctl('trip', 'edit', '3', '--wander', '0.4', '--radius', '10', '--direct')
        self.assertEqual(wandered.returncode, 0, wandered.stderr)
        self.assertEqual(self.server.hits[0][0:2], ('PATCH', '/api/v1/trip/stops/2'))
        self.assertEqual(self.server.hits[0][2]['dwell_mode'], 'still')
        self.assertFalse(self.server.hits[0][2]['wander'])
        self.assertEqual(self.server.hits[1][0:2], ('PATCH', '/api/v1/trip/stops/3'))
        self.assertEqual(self.server.hits[1][2]['wander_mps'], 0.4)
        self.assertEqual(self.server.hits[1][2]['wander_radius_m'], 10)
        self.assertTrue(self.server.hits[1][2]['direct'])


if __name__ == '__main__':
    unittest.main()
