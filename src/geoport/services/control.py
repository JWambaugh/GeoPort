"""Local automation API for scripts and assistants.

``/api/v1/*`` answers only on loopback unless ``GEOPORT_API_ALLOW_REMOTE`` is
set. The map UI keeps using the existing routes and Socket.IO, which are what
make an API-started drive show up on the map.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

import requests
from flask import jsonify, request

from geoport.config import logger
from geoport.core.models import Coordinate
from geoport.services.accuracy import MAX_ACCURACY_M
from geoport.services.drive import USER_AGENT, DriveRouteError, _google_key_from, _stops_enabled, _stops_seed

_LOOPBACK = {'127.0.0.1', '::1', 'localhost'}


def api_access_allowed(remote_addr: Optional[str]) -> bool:
    if os.getenv('GEOPORT_API_ALLOW_REMOTE', '').strip().lower() in ('1', 'true', 'yes', 'on'):
        return True
    return (remote_addr or '') in _LOOPBACK


def geocode_address(query: str, http: Any = None, limit: int = 5) -> list[dict[str, Any]]:
    text = (query or '').strip()
    if not text:
        raise DriveRouteError('Address is empty')
    client = http if http is not None else requests
    response = client.get(
        'https://nominatim.openstreetmap.org/search',
        params={'q': text, 'format': 'json', 'limit': max(1, min(int(limit), 8))},
        headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'},
        timeout=(3.05, 12),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        raise DriveRouteError(f'No locations found for "{text}"', status=404)
    results = []
    for item in payload:
        try:
            lat = float(item['lat'])
            lng = float(item['lon'])
            Coordinate(latitude=lat, longitude=lng).validate()
        except (KeyError, TypeError, ValueError):
            continue
        results.append({
            'lat': lat,
            'lng': lng,
            'display_name': item.get('display_name') or text,
        })
    if not results:
        raise DriveRouteError(f'No locations found for "{text}"', status=404)
    return results


def _as_place(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {'address': value}
    if isinstance(value, dict):
        return value
    raise DriveRouteError('A place must be {lat, lng} or {address}')


def resolve_place(value: Any, geocoder: Callable[[str], list[dict[str, Any]]]) -> tuple[float, float, str]:
    place = _as_place(value)
    if place.get('lat') is not None or place.get('lng') is not None:
        try:
            lat = float(place['lat'])
            lng = float(place['lng'])
        except (KeyError, TypeError, ValueError) as exc:
            raise DriveRouteError('lat and lng must be numbers') from exc
        Coordinate(latitude=lat, longitude=lng).validate()
        label = place.get('label') or f'{lat:.5f}, {lng:.5f}'
        return lat, lng, str(label)
    address = place.get('address') or place.get('q') or place.get('query')
    if not address:
        raise DriveRouteError('Place needs lat/lng or address')
    hits = geocoder(str(address))
    hit = hits[0]
    return float(hit['lat']), float(hit['lng']), str(hit.get('display_name') or address)


def resolve_endpoints(
    body: dict[str, Any],
    position: Any,
    geocoder: Callable[[str], list[dict[str, Any]]],
) -> tuple[tuple[float, float], tuple[float, float], str]:
    """Start defaults to the current simulated fix. Destination is required."""
    destination = body.get('to')
    if destination is None:
        destination = body.get('destination')
    if destination is None and body.get('address'):
        destination = {'address': body.get('address')}
    if destination is None and body.get('lat') is not None:
        destination = {'lat': body.get('lat'), 'lng': body.get('lng')}
    if destination is None:
        raise DriveRouteError('Destination is required (to: {lat, lng} or {address})')
    end_lat, end_lng, end_label = resolve_place(destination, geocoder)
    origin = body.get('from')
    if origin is None:
        origin = body.get('start_at')
    if origin is None:
        current = position.current()
        if current is None:
            raise DriveRouteError(
                'No simulated location yet. Pass from: {lat, lng}, or simulate a position first.',
            )
        return current, (end_lat, end_lng), end_label
    start_lat, start_lng, _label = resolve_place(origin, geocoder)
    return (start_lat, start_lng), (end_lat, end_lng), end_label


def _trip_summary(app: Any) -> Optional[dict[str, Any]]:
    trip = app.extensions.get('geoport', {}).get('trip')
    if trip is None:
        return None
    try:
        return trip.summary()
    except Exception:
        return None


def _stop_active_trip(app: Any) -> None:
    trip = app.extensions.get('geoport', {}).get('trip')
    if trip is not None and getattr(trip, 'active', False):
        trip.stop(user=True)


def register_control_api(
    app: Any,
    runtime: Any,
    session: Any,
    scanner: Any,
    position: Any,
    drive: Any,
    emit: Callable[[str, dict[str, Any]], None],
    http: Any = None,
) -> None:
    """Register ``/api/v1`` (loopback) and the UI accuracy route."""

    def device_info() -> Optional[dict[str, Any]]:
        snap = session.snapshot
        if not snap.connected:
            return None
        return {'udid': snap.udid, 'name': snap.device_name}

    def geocoder(query: str) -> list[dict[str, Any]]:
        return geocode_address(query, http=http)

    def status_payload() -> dict[str, Any]:
        snap = session.snapshot
        fix = position.snapshot()
        drive_status = drive.status()
        link = app.extensions['geoport'].get('autoconnect')
        link_state = link.snapshot() if link is not None else {
            'reconnecting': False,
            'last_disconnect_at': None,
            'reconnect_attempts': 0,
        }
        position_block = None
        if fix.get('lat') is not None:
            position_block = {
                'lat': fix['lat'],
                'lng': fix['lng'],
                'source': fix.get('source'),
                'updated_at': fix.get('updated_at'),
            }
        return {
            'ok': True,
            'connected': bool(snap.connected),
            'simulating': bool(snap.simulating),
            'device': {
                'udid': fix.get('udid') or snap.udid,
                'name': fix.get('device_name') or snap.device_name,
                'connected': bool(snap.connected),
            },
            'position': position_block,
            'accuracy_m': fix.get('accuracy_m') or 0,
            'accuracy_max_m': MAX_ACCURACY_M,
            'drive': drive_status,
            'trip': _trip_summary(app),
            'reconnecting': link_state.get('reconnecting', False),
            'last_disconnect_at': link_state.get('last_disconnect_at'),
            'reconnect_attempts': link_state.get('reconnect_attempts', 0),
        }

    def deny() -> Any:
        if api_access_allowed(request.remote_addr):
            return None
        return jsonify({
            'ok': False,
            'error': 'The automation API listens on localhost only. Set GEOPORT_API_ALLOW_REMOTE=1 to override.',
        }), 403

    def body() -> dict[str, Any]:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            raise DriveRouteError('JSON object expected')
        return data

    def push_device(fix: dict[str, Any]) -> None:
        if fix.get('lat') is None or not session.snapshot.connected:
            return
        try:
            runtime.run(session.set_location(lat=fix['lat'], lng=fix['lng']), timeout=25.0)
        except Exception as exc:
            logger.warning(f'Could not push simulated position to the device: {exc}')

    @app.before_request
    def _automation_api_is_local() -> Any:
        if request.path.startswith('/api/v1/'):
            return deny()
        return None

    @app.route('/api/v1/status', methods=['GET'])
    def automation_status() -> Any:
        return jsonify(status_payload())

    @app.route('/api/v1/geocode', methods=['GET'])
    def automation_geocode() -> Any:
        try:
            hits = geocoder(request.args.get('q') or request.args.get('address') or '')
            return jsonify({'ok': True, 'results': hits})
        except DriveRouteError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), exc.status
        except Exception as exc:
            logger.info(f'Geocode failed: {exc}')
            return jsonify({'ok': False, 'error': 'Geocoding failed'}), 502

    @app.route('/api/v1/teleport', methods=['POST'])
    def automation_teleport() -> Any:
        try:
            payload = body()
            place = payload.get('to') or payload.get('place') or payload
            lat, lng, label = resolve_place(place, geocoder)
            _stop_active_trip(app)
            fix = position.note(lat, lng, source='teleport', device=device_info(), pan=True)
            push_device(fix)
            drive.stop(user=False)
            result = status_payload()
            result['label'] = label
            return jsonify(result)
        except DriveRouteError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), exc.status
        except Exception as exc:
            logger.info(f'Teleport failed: {exc}')
            return jsonify({'ok': False, 'error': 'Teleport failed'}), 502

    @app.route('/api/v1/drive', methods=['POST'])
    def automation_drive() -> Any:
        try:
            payload = body()
            _stop_active_trip(app)
            if payload.get('accuracy_m') is not None:
                position.set_accuracy(float(payload['accuracy_m']), device=device_info())
            start, end, label = resolve_endpoints(payload, position, geocoder)
            offset = float(payload.get('speed_offset_pct', 0))
            plan = drive.plan(
                start,
                end,
                offset_pct=offset,
                provider=str(payload.get('provider') or 'osrm'),
                google_api_key=_google_key_from(payload),
                include_stops=_stops_enabled(payload),
                stops_seed=_stops_seed(payload),
            )
            public = plan.to_public_dict()
            public['start'] = {'lat': start[0], 'lng': start[1]}
            public['end'] = {'lat': end[0], 'lng': end[1], 'label': label}
            do_start = payload.get('start', True)
            if isinstance(do_start, str):
                do_start = do_start.strip().lower() not in ('0', 'false', 'no')
            public['started'] = bool(do_start)
            emit('drive_plan', public)
            drive_status = drive.start(offset) if do_start else drive.status()
            return jsonify({
                'ok': True,
                'from': {'lat': start[0], 'lng': start[1]},
                'to': {'lat': end[0], 'lng': end[1], 'label': label},
                'plan': public,
                'drive': drive_status,
            })
        except DriveRouteError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), exc.status
        except Exception as exc:
            logger.error(f'/api/v1/drive failed: {exc}')
            return jsonify({'ok': False, 'error': 'Could not plan a driving route'}), 502

    @app.route('/api/v1/drive/start', methods=['POST'])
    def automation_drive_start() -> Any:
        try:
            payload = body()
            offset = payload.get('speed_offset_pct')
            return jsonify(drive.start(None if offset is None else float(offset)))
        except DriveRouteError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), exc.status
        except (TypeError, ValueError) as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400

    @app.route('/api/v1/drive/pause', methods=['POST'])
    def automation_drive_pause() -> Any:
        return jsonify(drive.pause())

    @app.route('/api/v1/drive/resume', methods=['POST'])
    def automation_drive_resume() -> Any:
        try:
            return jsonify(drive.resume())
        except DriveRouteError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), exc.status

    @app.route('/api/v1/drive/stop', methods=['POST'])
    def automation_drive_stop() -> Any:
        return jsonify(drive.stop(user=True))

    def _set_accuracy(meters: float) -> dict[str, Any]:
        fix = position.set_accuracy(meters, device=device_info())
        if fix.get('lat') is not None and session.snapshot.connected and session.snapshot.simulating:
            try:
                runtime.run(session.set_location(lat=fix['lat'], lng=fix['lng']), timeout=25.0)
            except Exception as exc:
                logger.warning(f'Accuracy update did not reach the device: {exc}')
        result = status_payload()
        return result

    @app.route('/api/v1/accuracy', methods=['GET', 'POST'])
    @app.route('/api/accuracy', methods=['GET', 'POST'])
    def automation_accuracy() -> Any:
        try:
            if request.method == 'GET':
                return jsonify({
                    'ok': True,
                    'accuracy_m': position.snapshot().get('accuracy_m') or 0,
                    'max_m': MAX_ACCURACY_M,
                })
            if request.path.startswith('/api/v1/'):
                denied = deny()
                if denied is not None:
                    return denied
            payload = body()
            meters = payload.get('meters', payload.get('accuracy_m', payload.get('m')))
            if meters is None:
                raise DriveRouteError('meters is required')
            return jsonify(_set_accuracy(float(meters)))
        except DriveRouteError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), exc.status
        except (TypeError, ValueError) as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400

    @app.route('/api/v1/connect', methods=['POST'])
    def automation_connect() -> Any:
        connect_now = app.extensions['geoport'].get('connect_now')
        if connect_now is None:
            return jsonify({'ok': False, 'error': 'Auto-connect is not running'}), 503
        try:
            connect_now(True)
        except Exception as exc:
            logger.warning(f'/api/v1/connect failed: {exc}')
            return jsonify({'ok': False, 'error': str(exc), **{k: status_payload().get(k) for k in ('reconnecting', 'reconnect_attempts', 'last_disconnect_at')}}), 502
        return jsonify(status_payload())

    @app.route('/api/v1/devices', methods=['GET'])
    def automation_devices() -> Any:
        try:
            active = session.snapshot.udid if session.snapshot.connected else None
            devices = runtime.run(scanner.discover_devices(active_session_udid=active), timeout=30.0)
            return jsonify({'ok': True, 'devices': devices})
        except Exception as exc:
            logger.exception(f'/api/v1/devices failed: {exc}')
            return jsonify({'ok': False, 'error': str(exc)}), 500

    @app.route('/api/v1/clear', methods=['POST'])
    def automation_clear() -> Any:
        _stop_active_trip(app)
        drive.stop(user=True)
        position.clear()
        if session.snapshot.connected:
            try:
                runtime.run(session.clear_location(), timeout=20.0)
            except Exception as exc:
                logger.warning(f'Clearing device location failed: {exc}')
                return jsonify({'ok': False, 'error': str(exc), 'position': None}), 500
        return jsonify(status_payload())

    app.extensions['geoport']['control_status'] = status_payload
