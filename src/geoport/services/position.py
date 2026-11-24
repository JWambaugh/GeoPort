"""The simulated position GeoPort last committed to.

This is the single source of truth for "where the phone is" in the app: the
last fix published from a teleport, a drive tick, GPX playback, a nudge, or
the accuracy wander. The map pin shows this same point. It is written to
disk so a restart, a new drive, or an assistant command can continue from it.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from geoport.config import get_geoport_state_dir, logger
from geoport.core.models import Coordinate
from geoport.services.accuracy import GpsAccuracyModel
from geoport.services.drive import EARTH_RADIUS_M

OnFix = Callable[[dict[str, Any]], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def meters_to_degrees(lat: float, north_m: float, east_m: float) -> tuple[float, float]:
    dlat = (north_m / EARTH_RADIUS_M) * (180.0 / math.pi)
    cos = max(0.2, math.cos(math.radians(lat)))
    dlng = (east_m / (EARTH_RADIUS_M * cos)) * (180.0 / math.pi)
    return dlat, dlng


def degrees_to_meters(lat: float, dlat: float, dlng: float) -> tuple[float, float]:
    north = dlat * EARTH_RADIUS_M * math.pi / 180.0
    cos = max(0.2, math.cos(math.radians(lat)))
    east = dlng * EARTH_RADIUS_M * cos * math.pi / 180.0
    return north, east


class PositionService:
    """Tracks, jitters, and persists the current simulated fix."""

    def __init__(
        self,
        path: Optional[Path] = None,
        on_fix: Optional[OnFix] = None,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[Any] = None,
    ) -> None:
        self.path = Path(path) if path is not None else get_geoport_state_dir() / 'simulated_position.json'
        self._on_fix = on_fix
        self._clock = clock
        self._lock = threading.Lock()
        self._accuracy = GpsAccuracyModel(rng=rng)
        self.lat: Optional[float] = None
        self.lng: Optional[float] = None
        self.true_lat: Optional[float] = None
        self.true_lng: Optional[float] = None
        self.udid: Optional[str] = None
        self.device_name: Optional[str] = None
        self.source: Optional[str] = None
        self.updated_at: Optional[str] = None
        self._last_step = self._clock()
        self._last_save = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._load()

    def set_on_fix(self, on_fix: Optional[OnFix]) -> None:
        self._on_fix = on_fix

    def current(self) -> Optional[tuple[float, float]]:
        """The fix the pin and the phone are on, after accuracy wander."""
        with self._lock:
            if self.lat is None or self.lng is None:
                return None
            return (self.lat, self.lng)

    def note(
        self,
        lat: float,
        lng: float,
        source: str,
        device: Optional[dict[str, Any]] = None,
        step: bool = True,
        pan: bool = False,
    ) -> dict[str, Any]:
        """Remember ``lat, lng`` as the true point and publish the wandered fix."""
        Coordinate(latitude=float(lat), longitude=float(lng)).validate()
        fix = self._apply(float(lat), float(lng), source, device, step=step)
        if pan:
            fix['pan'] = True
        self._emit(fix)
        return fix

    def set_accuracy(self, meters: float, device: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        with self._lock:
            self._accuracy.set_meters(meters)
            true_lat, true_lng = self.true_lat, self.true_lng
        if true_lat is None or true_lng is None:
            self._save(force=True)
            fix = self.snapshot()
            self._emit(fix)
            return fix
        fix = self.note(true_lat, true_lng, source='accuracy', device=device, step=True)
        self._save(force=True)
        return fix

    def clear(self) -> dict[str, Any]:
        with self._lock:
            self.lat = None
            self.lng = None
            self.true_lat = None
            self.true_lng = None
            self.source = None
            self.updated_at = _now_iso()
            self._accuracy.set_meters(self._accuracy.accuracy_m)
            self._accuracy.north_m = 0.0
            self._accuracy.east_m = 0.0
        self._save(force=True)
        fix = self.snapshot()
        self._emit(fix)
        return fix

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            accuracy = self._accuracy.accuracy_m
            if self.lat is None or self.lng is None:
                return {
                    'lat': None,
                    'lng': None,
                    'accuracy_m': accuracy,
                    'cleared': True,
                    'udid': self.udid,
                    'device_name': self.device_name,
                    'source': self.source,
                    'updated_at': self.updated_at,
                }
            return {
                'lat': round(self.lat, 7),
                'lng': round(self.lng, 7),
                'true_lat': None if self.true_lat is None else round(self.true_lat, 7),
                'true_lng': None if self.true_lng is None else round(self.true_lng, 7),
                'accuracy_m': accuracy,
                'cleared': False,
                'udid': self.udid,
                'device_name': self.device_name,
                'source': self.source,
                'updated_at': self.updated_at,
            }

    def start_wander(self, playing: Callable[[], bool], device: Callable[[], Optional[dict[str, Any]]]) -> None:
        """Keep a stationary fix wandering while accuracy is above zero."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._playing = playing
        self._device = device

        def _loop() -> None:
            while not self._stop.wait(0.5):
                try:
                    if self._playing():
                        continue
                    with self._lock:
                        accuracy = self._accuracy.accuracy_m
                        true_lat, true_lng = self.true_lat, self.true_lng
                    if accuracy <= 0.0 or true_lat is None or true_lng is None:
                        continue
                    fix = self.note(true_lat, true_lng, source='accuracy', device=self._device())
                    if fix.get('lat') is None:
                        continue
                except Exception as exc:
                    logger.debug(f'Accuracy wander skipped: {exc}')

        self._thread = threading.Thread(target=_loop, name='geoport-accuracy', daemon=True)
        self._thread.start()

    def flush(self) -> None:
        self._save(force=True)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._save(force=True)

    def _apply(
        self,
        lat: float,
        lng: float,
        source: str,
        device: Optional[dict[str, Any]],
        step: bool,
    ) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            dt = max(0.0, now - self._last_step) if step else 0.0
            self._last_step = now
            if step:
                self._accuracy.step(dt)
            elif self._accuracy.accuracy_m <= 0.0:
                self._accuracy.north_m = 0.0
                self._accuracy.east_m = 0.0
            north, east = self._accuracy.offset()
            dlat, dlng = meters_to_degrees(lat, north, east)
            self.true_lat = lat
            self.true_lng = lng
            self.lat = lat + dlat
            self.lng = lng + dlng
            self.source = source
            self.updated_at = _now_iso()
            if device:
                if device.get('udid'):
                    self.udid = device.get('udid')
                if device.get('name'):
                    self.device_name = device.get('name')
            fix = {
                'lat': round(self.lat, 7),
                'lng': round(self.lng, 7),
                'true_lat': round(self.true_lat, 7),
                'true_lng': round(self.true_lng, 7),
                'accuracy_m': self._accuracy.accuracy_m,
                'cleared': False,
                'udid': self.udid,
                'device_name': self.device_name,
                'source': self.source,
                'updated_at': self.updated_at,
            }
        self._save(force=source in ('teleport', 'set', 'clear'))
        return fix

    def _emit(self, fix: dict[str, Any]) -> None:
        if self._on_fix is None:
            return
        try:
            self._on_fix(fix)
        except Exception as exc:
            logger.debug(f'Position listener failed: {exc}')

    def _save(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_save < 2.0:
            return
        self._last_save = now
        payload = self.snapshot()
        payload['true_lat'] = self.true_lat
        payload['true_lng'] = self.true_lng
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix='.pos-', suffix='.json')
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.debug(f'Could not persist simulated position: {exc}')

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        self._accuracy.set_meters(float(raw.get('accuracy_m') or 0.0))
        self.udid = raw.get('udid')
        self.device_name = raw.get('device_name')
        self.source = raw.get('source')
        self.updated_at = raw.get('updated_at')
        if raw.get('lat') is None or raw.get('lng') is None:
            return
        try:
            lat = float(raw['lat'])
            lng = float(raw['lng'])
            Coordinate(latitude=lat, longitude=lng).validate()
        except (TypeError, ValueError):
            return
        true_lat = float(raw['true_lat']) if raw.get('true_lat') is not None else lat
        true_lng = float(raw['true_lng']) if raw.get('true_lng') is not None else lng
        self.lat = lat
        self.lng = lng
        self.true_lat = true_lat
        self.true_lng = true_lng
        if self._accuracy.accuracy_m > 0.0:
            north, east = degrees_to_meters(true_lat, lat - true_lat, lng - true_lng)
            self._accuracy.north_m = north
            self._accuracy.east_m = east

