"""Keep a paired iPhone or iPad connected.

On startup GeoPort connects to the last device that worked, or to the only
device it can see, preferring USB over Wi-Fi. If that tunnel drops while a
fix is set or a drive is in progress, a watchdog opens it again and puts the
current simulated position back. The drive clock is not paused for this.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from geoport.config import get_geoport_state_dir, logger

USB_BUCKETS = ('USB',)
WIFI_BUCKETS = ('Wifi', 'Wi-Fi', 'WiFi', 'Manual Wifi', 'Network')


def choose_device(devices: dict[str, Any], last_udid: Optional[str]) -> Optional[dict[str, Any]]:
    """Pick one discovery entry. USB wins over Wi-Fi for the chosen device."""
    if not isinstance(devices, dict) or not devices:
        return None
    udids = [key for key in devices if key and key != 'error']
    target = last_udid if last_udid and last_udid in devices else None
    if target is None and len(udids) == 1:
        target = udids[0]
    if target is None:
        return None
    buckets = devices.get(target) or {}
    if not isinstance(buckets, dict):
        return None
    for name in USB_BUCKETS:
        entries = buckets.get(name) or []
        if entries:
            return entries[0]
    for name in list(buckets.keys()):
        if str(name).upper() == 'USB' and buckets.get(name):
            return buckets[name][0]
    for name in WIFI_BUCKETS:
        entries = buckets.get(name) or []
        if entries:
            return entries[0]
    for entries in buckets.values():
        if entries:
            return entries[0]
    return None


def should_reconnect(
    *,
    connected: bool,
    user_hold: bool,
    has_position: bool,
    drive_state: str,
    has_last_udid: bool,
    startup: bool,
) -> bool:
    if connected or user_hold:
        return False
    if startup:
        return True
    if has_position or drive_state in ('playing', 'paused'):
        return True
    return bool(has_last_udid)


class AutoConnect:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else get_geoport_state_dir() / 'last_device.json'
        self.reconnecting = False
        self.last_disconnect_at: Optional[str] = None
        self.reconnect_attempts = 0
        self.user_hold = False
        self.last_udid: Optional[str] = None
        self.last_error: Optional[str] = None
        self._backoff = 2.0
        self._next_try = 0.0
        self._startup = True
        self._was_connected = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._load()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                'reconnecting': self.reconnecting,
                'last_disconnect_at': self.last_disconnect_at,
                'reconnect_attempts': self.reconnect_attempts,
                'auto': not self.user_hold,
                'last_udid': self.last_udid,
                'last_error': self.last_error,
            }

    def remember(self, udid: str, name: Optional[str] = None) -> None:
        with self._lock:
            self.last_udid = udid
            self.user_hold = False
            self.reconnecting = False
            self.reconnect_attempts = 0
            self._backoff = 2.0
            self.last_error = None
            self._startup = False
        self._save(udid, name)

    def hold(self) -> None:
        """The user asked to disconnect. Do not connect again until they do."""
        with self._lock:
            self.user_hold = True
            self.reconnecting = False

    def release_hold(self) -> None:
        with self._lock:
            self.user_hold = False

    def start(self, tick: Callable[[], None], interval_s: float = 4.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.wait(interval_s):
                try:
                    tick()
                except Exception as exc:
                    logger.warning(f'Auto-connect tick failed: {exc}')

        self._thread = threading.Thread(target=_loop, name='geoport-autoconnect', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)

    def note_disconnect(self, when_iso: str) -> None:
        with self._lock:
            if self.last_disconnect_at is None:
                self.last_disconnect_at = when_iso
            self.reconnecting = not self.user_hold

    def note_failure(self, message: str, now: float) -> float:
        with self._lock:
            self.reconnect_attempts += 1
            self.last_error = message
            self.reconnecting = True
            wait = self._backoff
            self._backoff = min(30.0, self._backoff * 2.0)
            self._next_try = now + wait
            self._startup = False
            return wait

    def note_success(self) -> None:
        with self._lock:
            self.reconnecting = False
            self.reconnect_attempts = 0
            self._backoff = 2.0
            self.last_error = None
            self.last_disconnect_at = None
            self._startup = False
            self._was_connected = True

    def due(self, now: float) -> bool:
        with self._lock:
            return now >= self._next_try

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict) and raw.get('udid'):
            self.last_udid = str(raw['udid'])

    def _save(self, udid: str, name: Optional[str]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({'udid': udid, 'name': name}), encoding='utf-8')
        except OSError as exc:
            logger.debug(f'Could not save last device: {exc}')
