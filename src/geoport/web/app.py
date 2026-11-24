# Hand-repaired from GeoPort v5.0.3 bytecode (see docs/RECOVERY.md).
"""Flask + Socket.IO application factory and HTTP/WebSocket routes for GeoPort v5."""
from __future__ import annotations
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from flask import Flask, Response, jsonify, make_response, render_template, request
from flask_socketio import SocketIO
from jinja2 import ChoiceLoader, FileSystemLoader
import requests
from geoport.config import APP_VERSION_NUMBER, APP_VERSION_TYPE, BASE_DIR, DEFAULT_FUEL_REGION, PLATFORM_NAME, get_geoport_state_dir, logger
from geoport.core.developer_mode import enable_developer_mode
from geoport.core.mounter import mount_developer_image
from geoport.core.pair_records import export_pairing_bytes_async
from geoport.core.session import DeveloperModeRequiredError, DeviceSessionManager
from geoport.discovery.scanner import DeviceScanner
from geoport.runtime import AsyncRuntime, get_runtime
from geoport.services.control import register_control_api
from geoport.services.drive import register_drive_routes
from geoport.services.fuel import FuelService
from geoport.services.position import PositionService
from geoport.services.gpx import GPXService
from geoport.services.updates import MetadataService
_TILE_PROVIDERS: dict[str, tuple[str, str]] = {
    'osm': ('https://tile.openstreetmap.org/{z}/{x}/{y}.png', 'image/png'),
    'hot': ('https://a.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png', 'image/png'),
    'cyclosm': ('https://a.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png', 'image/png'),
    'topo': ('https://a.tile.opentopomap.org/{z}/{x}/{y}.png', 'image/png'),
    'opnvkarte': ('https://tileserver.memomaps.de/tilegen/{z}/{x}/{y}.png', 'image/png'),
    'satellite': ('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', 'image/jpeg'),
    'esri_street': ('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}', 'image/jpeg'),
    'esri_topo': ('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}', 'image/jpeg'),
    'esri_natgeo': ('https://server.arcgisonline.com/ArcGIS/rest/services/NatGeo_World_Map/MapServer/tile/{z}/{y}/{x}', 'image/jpeg'),
    'esri_dark_base': ('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', 'image/jpeg'),
    'esri_dark_ref': ('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}', 'image/png'),
    'esri_light_base': ('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}', 'image/jpeg'),
    'esri_light_ref': ('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}', 'image/png'),
    'esri_trans_ref': ('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}', 'image/png'),
    'esri_places_ref': ('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', 'image/png')}
_TRANSPARENT_OVERLAY_STYLES = {
    'esri_dark_ref',
    'esri_light_ref',
    'esri_trans_ref',
    'esri_places_ref'}
_TILE_HTTP_HEADERS = {
    'User-Agent': 'GeoPort/5.0 (Self-Hosted Personal iOS Location Simulator; OpenStreetMap Attribution Displayed)',
    'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8'}

def create_app(runtime: Optional[AsyncRuntime] = None, session_mgr: Optional[DeviceSessionManager] = None, scanner: Optional[DeviceScanner] = None, fuel_svc: Optional[FuelService] = None, meta_svc: Optional[MetadataService] = None, manual_wifihost: Optional[str] = None, manual_udid: Optional[str] = None, on_shutdown: Optional[Callable[[], None]] = None) -> tuple[Flask, SocketIO]:
    """Create and configure the GeoPort v5 Flask + SocketIO application."""
    templates_dir = BASE_DIR / 'templates'
    static_dir = BASE_DIR / 'static'
    app = Flask(__name__, template_folder=str(templates_dir), static_folder=str(static_dir))
    app.config['SECRET_KEY'] = os.getenv('GEOPORT_SECRET_KEY', 'geoport-v5-secret')
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    template_search_paths = []
    for candidate in (get_geoport_state_dir() / 'templates', Path.cwd() / 'templates', Path(sys.executable).resolve().parent / 'templates', templates_dir):
        c_str = str(candidate)
        if candidate.is_dir() and c_str not in template_search_paths:
            template_search_paths.append(c_str)
    app.jinja_loader = ChoiceLoader([FileSystemLoader(p) for p in template_search_paths])
    socketio = SocketIO(app, async_mode='threading', cors_allowed_origins='*')
    rt = runtime or get_runtime()
    meta = meta_svc or MetadataService()
    fuel = fuel_svc or FuelService()
    tile_http = requests.Session()
    tile_http.headers.update(_TILE_HTTP_HEADERS)

    def emit_socket_event(event: str, payload: dict[str, Any]) -> None:
        try:
            socketio.emit(event, payload)
        except Exception as exc:
            logger.debug(f"SocketIO emit '{event}' failed: {exc}")
            return None

    sess = session_mgr or DeviceSessionManager(socket_emitter=emit_socket_event)
    dev_scanner = scanner or DeviceScanner(locale_provider=meta.get_user_country, manual_wifihost=manual_wifihost, manual_udid=manual_udid)
    position = PositionService()

    def _device_info() -> Optional[dict[str, Any]]:
        snap = sess.snapshot
        if not snap.connected:
            return None
        return {'udid': snap.udid, 'name': snap.device_name}

    app.extensions['geoport'] = {
        'runtime': rt,
        'session_mgr': sess,
        'scanner': dev_scanner,
        'fuel_svc': fuel,
        'meta_svc': meta,
        'position': position}
    register_drive_routes(app, rt, sess, emit_socket_event)
    register_control_api(app, rt, sess, dev_scanner, position, app.extensions['geoport']['drive'], emit_socket_event)

    def _on_fix(fix: dict[str, Any]) -> None:
        emit_socket_event('position', fix)
        if fix.get('source') != 'accuracy':
            return
        if fix.get('lat') is None or not sess.snapshot.connected or not sess.snapshot.simulating:
            return
        try:
            rt.submit(sess.set_location(lat=fix['lat'], lng=fix['lng']))
        except Exception as exc:
            logger.debug(f'Accuracy wander did not reach the device: {exc}')

    position.set_on_fix(_on_fix)
    position.start_wander(
        playing=lambda: app.extensions['geoport']['drive'].player.state == 'playing',
        device=_device_info,
    )
    last_selected_state = {
        'udid': manual_udid,
        'wifi_address': manual_wifihost}

    @app.route('/')
    def index() -> Response:
        fuel.warm_async()
        user_locale = meta.get_user_country()
        version_message, github_broadcast = meta.get_version_and_broadcast()
        requested_ui = (request.args.get('ui') or '').strip().lower()
        cookie_ui = (request.cookies.get('geoport_ui') or '').strip().lower()
        default_ui = os.getenv('GEOPORT_DEFAULT_UI', 'modern').strip().lower()
        if requested_ui in ('modern', 'classic'):
            active_ui = requested_ui
        elif cookie_ui in ('modern', 'classic'):
            active_ui = cookie_ui
        else:
            active_ui = default_ui if default_ui in ('modern', 'classic') else 'modern'
        template_name = 'map_modern.html' if active_ui == 'modern' else 'map.html'
        rendered = render_template(template_name, version_message=version_message, github_broadcast=github_broadcast, user_locale=user_locale, app_version_num=APP_VERSION_NUMBER, app_version=APP_VERSION_NUMBER, app_version_type=APP_VERSION_TYPE, error_message=None, current_platform=PLATFORM_NAME, ui_mode=active_ui, sudo_message='')
        resp = make_response(rendered)
        if requested_ui in ('modern', 'classic'):
            resp.set_cookie('geoport_ui', active_ui, max_age=31536000, samesite='Lax')
        return resp

    @app.route('/api/status')
    def api_status() -> Response:
        fix = position.snapshot()
        reported = None
        if fix.get('lat') is not None:
            reported = {
                'lat': fix['lat'],
                'lng': fix['lng'],
                'source': fix.get('source'),
                'updated_at': fix.get('updated_at'),
            }
        return jsonify({
            'version': APP_VERSION_NUMBER,
            'version_type': APP_VERSION_TYPE,
            'platform': PLATFORM_NAME,
            'rootless_userspace_rsd': True,
            'session': sess.snapshot.to_dict(),
            'position': reported,
            'accuracy_m': fix.get('accuracy_m') or 0,
            'drive': app.extensions['geoport']['drive'].status()})

    @app.route('/list_devices')
    def list_devices_route() -> Response:
        try:
            active_udid = sess.snapshot.udid if sess.snapshot.connected else None
            devices = rt.run(dev_scanner.discover_devices(active_session_udid=active_udid), timeout=30.0)
            return jsonify(devices)
        except Exception as exc:
            logger.exception(f'/list_devices failed: {exc}')
            return jsonify({
                'error': str(exc)})

    @app.route('/api/export_pairing/<udid>')
    def export_pairing_route(udid: str) -> Response | tuple[Response, int]:
        try:
            prefer_rp = (request.args.get('format') or '').strip().lower() == 'rppairing'
            xml_bytes, filename = rt.run(export_pairing_bytes_async(udid, prefer_rppairing=prefer_rp), timeout=10.0)
            return Response(xml_bytes, mimetype='application/octet-stream', headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Cache-Control': 'no-store'})
        except FileNotFoundError as exc:
            return (jsonify({
                'error': str(exc)}), 404)
        except ValueError as exc:
            return (jsonify({
                'error': str(exc)}), 400)
        except Exception as exc:
            logger.error(f'Pairing export failed for {udid}: {exc}')
            return (jsonify({
                'error': str(exc)}), 500)

    @app.route('/connect_device', methods=['POST'])
    def connect_device_route() -> Response:
        data = request.get_json(silent=True) or {}
        logger.info(f'/connect_device request: {data}')
        udid = data.get('udid') or last_selected_state.get('udid')
        if not udid:
            return jsonify({
                'error': 'No device UDID provided'})
        ios_version = data.get('ios_version')
        raw_conn_type = str(data.get('connType') or 'Network')
        is_usb = raw_conn_type.upper() == 'USB'
        conn_type = 'USB' if is_usb else 'Network'
        wifi_address = data.get('wifiAddress')
        wifi_port = data.get('wifiPort')
        wifi_endpoints = data.get('wifiEndpoints') or []
        if isinstance(wifi_endpoints, dict):
            wifi_endpoints = [wifi_endpoints]
        if not is_usb and not wifi_address and not wifi_endpoints:
            cached_eps = dev_scanner.get_cached_wifi_endpoints(udid)
            if cached_eps:
                wifi_endpoints = cached_eps
                wifi_address = cached_eps[0].get('wifiAddress')
                wifi_port = cached_eps[0].get('wifiPort')
            elif manual_wifihost:
                wifi_address = manual_wifihost
        last_selected_state['udid'] = udid
        if wifi_address:
            last_selected_state['wifi_address'] = wifi_address
        try:
            snapshot = rt.run(sess.connect(udid=udid, conn_type=conn_type, ios_version=ios_version, wifi_address=wifi_address, wifi_port=wifi_port, wifi_endpoints=wifi_endpoints), timeout=65.0)
            return jsonify({
                'rsd_data': snapshot.rsd_data,
                'message': f'Connected via {snapshot.transport}',
                'transport': snapshot.transport,
                'userspace_tunnel': snapshot.userspace_tunnel,
                'ios_version': snapshot.ios_version})
        except DeveloperModeRequiredError:
            return jsonify({
                'developer_mode_required': 'True'})
        except Exception as exc:
            msg = str(exc)
            logger.error(f'/connect_device failed for {udid}: {msg}')
            if 'No Pair Record' in msg:
                return jsonify({
                    'Error': 'No Pair Record Found',
                    'error': msg})
            return jsonify({
                'error': msg})

    @app.route('/stop_tunnel', methods=['POST'])
    @app.route('/disconnect', methods=['POST'])
    def stop_tunnel_route() -> Response:
        logger.info('Stopping active device tunnel')
        try:
            rt.run(sess.disconnect(clear_gps=True), timeout=20.0)
        except Exception as exc:
            logger.warning(f'Tunnel disconnect warning: {exc}')
        return jsonify('Tunnel stopped')

    @app.route('/enable_developer_mode', methods=['POST'])
    def enable_developer_mode_route() -> Response:
        data = request.get_json(silent=True) or {}
        udid = data.get('udid') or last_selected_state.get('udid')
        wifi_host = data.get('wifiAddress') or last_selected_state.get('wifi_address')
        try:
            (ok, err_msg, extra) = rt.run(enable_developer_mode(udid=udid, wifi_host=wifi_host), timeout=45.0)
            if ok:
                return jsonify({
                    'success': True,
                    'udid': udid,
                    **extra})
            return jsonify({
                'error': err_msg or 'Failed to enable Developer Mode',
                **extra})
        except Exception as exc:
            return jsonify({
                'error': str(exc)})

    @app.route('/mount_developer_image', methods=['POST'])
    def mount_developer_image_route() -> Response | str:
        data = request.get_json(silent=True) or {}
        udid = data.get('udid') or sess.snapshot.udid or last_selected_state.get('udid')
        wifi_host = sess.snapshot.wifi_address or last_selected_state.get('wifi_address')
        try:
            rt.run(mount_developer_image(udid=udid, wifi_host=wifi_host), timeout=120.0)
            return 'Developer image mounted successfully'
        except Exception as exc:
            return jsonify({
                'error': str(exc)})

    @app.route('/update_location', methods=['POST'])
    def update_location_route() -> Response | tuple[Response, int] | str:
        data = request.get_json(silent=True) or {}
        try:
            lat = float(data['lat'])
            lng = float(data['lng'])
            fix = position.note(lat, lng, source='pin', device=_device_info())
            sess.update_target_coordinate(fix['lat'], fix['lng'])
            if sess.snapshot.simulating and sess.snapshot.connected:
                rt.run(sess.set_location(lat=fix['lat'], lng=fix['lng']), timeout=25.0)
            return 'Location updated successfully'
        except (KeyError, TypeError, ValueError) as exc:
            return (jsonify({
                'error': f'Invalid coordinates: {exc}'}), 400)

    @app.route('/set_location', methods=['POST'])
    def set_location_route() -> Response | tuple[Response, int] | str:
        data = request.get_json(silent=True) or {}
        lat_raw = data.get('lat')
        lng_raw = data.get('lng')
        try:
            lat = float(lat_raw) if lat_raw is not None else None
            lng = float(lng_raw) if lng_raw is not None else None
            if lat is None or lng is None:
                if position.true_lat is not None and position.true_lng is not None:
                    lat, lng = position.true_lat, position.true_lng
            if lat is not None and lng is not None:
                fix = position.note(lat, lng, source='set', device=_device_info())
                lat, lng = fix['lat'], fix['lng']
            rt.run(sess.set_location(lat=lat, lng=lng), timeout=25.0)
            return 'Location set successfully'
        except ValueError as exc:
            return (jsonify({
                'error': str(exc)}), 400)
        except Exception as exc:
            msg = str(exc)
            logger.error(f'/set_location failed: {msg}')
            if 'No route to host' in msg:
                emit_socket_event('no_route_to_host', {
                    'message': msg})
            return (jsonify({
                'error': msg}), 500)

    @app.route('/stop_location', methods=['POST'])
    def stop_location_route() -> Response | tuple[Response, int] | str:
        try:
            rt.run(sess.clear_location(), timeout=20.0)
            return 'Location cleared successfully'
        except Exception as exc:
            logger.error(f'/stop_location failed: {exc}')
            return (jsonify({
                'error': str(exc)}), 500)

    @app.route('/upload_gpx', methods=['POST'])
    def upload_gpx_route() -> Response | tuple[Response, int]:
        file = request.files.get('gpxFile')
        velocity = request.form.get('velocity', 'walk')
        if file is None:
            return (jsonify({
                'error': 'No GPX file provided'}), 400)
        try:
            xml_text = file.read().decode('utf-8', errors='replace')
            points = GPXService.parse_gpx_content(xml_text)
            return jsonify({
                'success': True,
                'points': points,
                'count': len(points),
                'velocity': velocity})
        except Exception as exc:
            logger.error(f'/upload_gpx failed: {exc}')
            return (jsonify({
                'error': str(exc)}), 400)

    @app.route('/api/fuel_types')
    def get_fuel_types_route() -> Response | tuple[Response, int]:
        region = request.args.get('region', DEFAULT_FUEL_REGION)
        fuel_types = fuel.get_fuel_types(region=region)
        if fuel_types is None:
            return (jsonify([]), 503)
        return jsonify(fuel_types)

    @app.route('/api/data/<fuel_type>')
    def get_fuel_type_data_route(fuel_type: str) -> Response | tuple[Response, int]:
        region = request.args.get('region', DEFAULT_FUEL_REGION)
        selected, available = fuel.get_price_data(fuel_type=fuel_type, region=region)
        if not available:
            return (jsonify({}), 503)
        return jsonify(selected)

    @app.route('/api/map_layers')
    def get_map_layers_route() -> Response:
        force = request.args.get('refresh', '').strip().lower() in ('1', 'true', 'yes')
        return jsonify(meta.get_map_layers_config(force_refresh=force))

    @app.route('/api/tiles/<style>/<int:z>/<int:x>/<int:y>')
    def proxy_map_tile_route(style: str, z: int, x: int, y: int) -> Response | tuple[Response, int]:
        """Fast local disk-cached proxy for 100% free, zero-API-key OpenStreetMap, Esri & specialty raster tiles."""
        if style not in _TILE_PROVIDERS:
            style = 'osm'
        if z < 0 or z > 19 or x < 0 or y < 0 or x >= 1 << z or y >= 1 << z:
            return (jsonify({
                'error': 'Invalid tile coordinates'}), 400)
        tpl, media_type = _TILE_PROVIDERS[style]
        is_overlay = style in _TRANSPARENT_OVERLAY_STYLES
        min_bytes = 40 if is_overlay else 120
        cache_dir = get_geoport_state_dir() / 'tile_cache' / style / str(z)
        cache_file = cache_dir / f'{x}_{y}.tile'
        cache_headers = {
            'Cache-Control': 'public, max-age=604800, immutable'}
        if cache_file.is_file() and cache_file.stat().st_size >= min_bytes:
            try:
                return Response(cache_file.read_bytes(), mimetype=media_type, headers=cache_headers)
            except OSError:
                pass
        upstream_url = tpl.format(z=z, x=x, y=y)
        data = None
        ctype = media_type
        try:
            resp = tile_http.get(upstream_url, timeout=8)
            resp.raise_for_status()
            data = resp.content
            ctype = resp.headers.get('Content-Type') or media_type
        except Exception as exc:
            if not is_overlay and style != 'osm':
                fb_url = _TILE_PROVIDERS['osm'][0].format(z=z, x=x, y=y)
                try:
                    fb_resp = tile_http.get(fb_url, timeout=8)
                    fb_resp.raise_for_status()
                    data = fb_resp.content
                    ctype = 'image/png'
                except Exception as fb_exc:
                    return (jsonify({
                        'error': f'Tile fetch failed: {fb_exc}'}), 502)
            else:
                return (jsonify({
                    'error': f'Upstream tile server unreachable: {exc}'}), 502)
        if data and len(data) >= min_bytes:
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(data)
            except OSError:
                pass
        return Response(data or b'', mimetype=ctype, headers=cache_headers)

    @app.route('/exit', methods=['POST'])
    def exit_app_route() -> Response:
        logger.warning('Exit requested via /exit — tearing down active session and stopping GeoPort')

        def _graceful_stop() -> None:
            drive = app.extensions['geoport'].get('drive')
            tracked = app.extensions['geoport'].get('position')
            if drive is not None:
                try:
                    drive.stop()
                    drive.close()
                except Exception:
                    pass
            if tracked is not None:
                try:
                    tracked.stop()
                except Exception:
                    pass
            try:
                rt.run(sess.disconnect(clear_gps=True), timeout=5.0)
            except Exception:
                pass
            if on_shutdown is not None:
                try:
                    on_shutdown()
                except Exception:
                    pass
            rt.shutdown(timeout=2.0)
            if sys.platform == 'win32':
                time.sleep(0.15)
                os._exit(0)
                return None
            os.kill(os.getpid(), signal.SIGINT)

        threading.Timer(0.25, _graceful_stop).start()
        return jsonify({
            'success': True,
            'message': 'GeoPort server is shutting down...'})

    return (app, socketio)
