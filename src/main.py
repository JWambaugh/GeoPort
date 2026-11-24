# Hand-repaired from GeoPort v5.0.3 bytecode (see docs/RECOVERY.md).
"""GeoPort v5 Entrypoint — Rootless Cross-Platform iOS Location Simulation Suite.

Runs without sudo/Administrator on Linux (including unprivileged Proxmox LXCs),
macOS (Apple Silicon & Intel), and Windows 10/11 via pymobiledevice3 Userspace RSD Tunnels.
"""
from __future__ import annotations
import argparse
import multiprocessing
import os
import random
import signal
import socket
import sys
import threading
import time
import webbrowser
from typing import Optional
APP_VERSION_NUMBER = '5.0.3'
APP_VERSION_TYPE = 'fuel'
def _close_pyinstaller_splash() -> None:
    if sys.platform == 'win32':
        try:
            import pyi_splash
            pyi_splash.update_text('GeoPort v5 Loaded...')
            pyi_splash.close()
        except Exception:
            return None
def is_port_in_use(port: int, host: str='127.0.0.1') -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0
def select_listen_port(explicit_port: Optional[int], default_port: int=54321) -> int:
    """Return `explicit_port` if requested, otherwise `default_port` (falling back to a free port if busy)."""
    if explicit_port is not None:
        return int(explicit_port)
    else:
        if not is_port_in_use(default_port):
            return default_port
        else:
            for _ in range(30):
                candidate = random.randint(49215, 65535)
                if not is_port_in_use(candidate):
                    return candidate
            return default_port
def parse_args(argv: Optional[list[str]]=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='GeoPort v5 - Rootless iOS Location Simulator')
    parser.add_argument('--no-browser', action='store_true', help='Do not automatically open the web browser')
    parser.add_argument('--port', type=int, default=None, help='HTTP port to bind (default: 54321)')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Bind address (default: 0.0.0.0)')
    parser.add_argument('--wifihost', type=str, default=None, help='Explicit iOS Wi-Fi IP/hostname to target')
    parser.add_argument('--udid', type=str, default=None, help='Explicit iOS device UDID to target')
    parser.add_argument('--self-test', action='store_true', help='Verify all frozen imports, metadata, templates, and Flask routes, then exit')
    return parser.parse_args(argv)
def _run_self_test() -> None:
    """Import the full runtime stack and smoke-test Flask routes inside the frozen binary."""
    # Control flow reconstructed by hand from pycdc + PyLingual output.
    import importlib
    import importlib.metadata
    from geoport.core import developer_mode, models, mounter, pair_records, session
    from geoport.discovery import mdns_advertiser, scanner
    from geoport.runtime import get_runtime
    from geoport.web.app import create_app
    import pmd_pytcp
    import pymobiledevice3.remote.userspace_tunnel
    import pymobiledevice3.services.dvt.instruments.location_simulation
    for pkg in ('werkzeug', 'flask', 'pymobiledevice3', 'pycountry', 'pmd-pytcp'):
        importlib.metadata.version(pkg)
    if sys.platform == 'darwin':
        importlib.import_module('apple_compress')
        importlib.metadata.version('apple_compress')
    runtime = get_runtime()
    os.environ['GEOPORT_AUTOCONNECT'] = '0'
    os.environ['GEOPORT_TRIP_RESUME'] = '0'
    try:
        app, _socketio = create_app(runtime=runtime)
        with app.test_client() as client:
            for path in ('/', '/?ui=classic', '/api/map_layers'):
                resp = client.get(path)
                if resp.status_code != 200:
                    raise RuntimeError(f'Self-test GET {path} returned HTTP {resp.status_code}')
    finally:
        runtime.shutdown(timeout=2.0)
    print(f'GeoPort v{APP_VERSION_NUMBER} self-test PASSED ({sys.platform})')
def main(argv: Optional[list[str]]=None) -> None:
    multiprocessing.freeze_support()
    from geoport.runtime import ensure_platform_event_loop_policy, get_runtime
    ensure_platform_event_loop_policy()
    _close_pyinstaller_splash()
    args = parse_args(argv)
    if args.self_test:
        _run_self_test()
        return
    else:
        from geoport.config import DEFAULT_PORT, logger
        from geoport.discovery.mdns_advertiser import MDNSAdvertiser
        from geoport.web.app import create_app
        chosen_port = select_listen_port(args.port, default_port=DEFAULT_PORT)
        mdns = MDNSAdvertiser(port=chosen_port)
        lan_ip = mdns.start()
        runtime = get_runtime()
        app, socketio = create_app(runtime=runtime, manual_wifihost=args.wifihost, manual_udid=args.udid, on_shutdown=mdns.stop)
        sess = app.extensions['geoport']['session_mgr']
        def _handle_sig(signum: int, _frame: object) -> None:
            logger.info(f'Received signal {signum}; shutting down GeoPort v5 cleanly...')
            mdns.stop()
            try:
                runtime.run(sess.disconnect(clear_gps=False), timeout=3.0)
            except Exception:
                pass
            runtime.shutdown(timeout=2.0)
            if sys.platform == 'win32':
                os._exit(0)
            sys.exit(0)
        signal.signal(signal.SIGINT, _handle_sig)
        if hasattr(signal, 'SIGTERM'):
            signal.signal(signal.SIGTERM, _handle_sig)
        logger.info(f'GeoPort v{APP_VERSION_NUMBER} (Rootless Userspace RSD) listening on http://{args.host}:{chosen_port}' + (f' (LAN: http://{lan_ip}:{chosen_port} / http://geoport.local:{chosen_port})' if lan_ip else ''))
        if not args.no_browser:
            def _open() -> None:
                time.sleep(1.2)
                try:
                    webbrowser.open(f'http://localhost:{chosen_port}')
                except Exception as exc:
                    logger.debug(f'Could not auto-open browser: {exc}')
            threading.Thread(target=_open, daemon=True, name='geoport-browser-opener').start()
        else:
            logger.info('Running in headless mode (--no-browser)')
        try:
            socketio.run(app, host=args.host, port=chosen_port, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
        finally:
            mdns.stop()
            runtime.shutdown(timeout=2.0)
if __name__ == '__main__':
    main()