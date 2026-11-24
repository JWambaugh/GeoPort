"""Central configuration, constants, environment settings, and logging for GeoPort v5."""
from __future__ import annotations
import logging
import os
import sys
from pathlib import Path
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
APP_VERSION_NUMBER = '5.0.3'
APP_VERSION = APP_VERSION_NUMBER
APP_VERSION_TYPE = 'fuel'
DEFAULT_PORT = 54321
DEFAULT_HOST = '0.0.0.0'
LOCKDOWN_TCP_PORT = 62078
GITHUB_REPO = os.getenv('GEOPORT_GITHUB_REPO', 'davesc63/GeoPort')
CURRENT_VERSION_FILE = 'CURRENT_VERSION'
BROADCAST_FILE = 'BROADCAST'
MAP_LAYERS_FILE = 'MAP_LAYERS.json'
FUEL_API_URL = os.getenv('GEOPORT_FUEL_API_URL', 'https://projectzerothree.info/api.php?format=json')
FUEL_CACHE_TTL_SECONDS = float(os.getenv('GEOPORT_FUEL_CACHE_TTL', '600'))
DEFAULT_FUEL_REGION = os.getenv('GEOPORT_DEFAULT_FUEL_REGION', 'QLD')
DEFAULT_FUEL_TYPE = os.getenv('GEOPORT_DEFAULT_FUEL_TYPE', 'U98')
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    BASE_DIR = Path(getattr(sys, '_MEIPASS')).resolve()
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
CURRENT_PLATFORM = sys.platform
PLATFORM_NAME = {'win32': 'Windows', 'linux': 'Linux', 'darwin': 'MacOS'}.get(CURRENT_PLATFORM, 'Unknown')
def get_geoport_state_dir() -> Path:
    """Return user-writable directory (~/.geoport) for persistent metadata cache."""
    override = os.getenv('GEOPORT_STATE_DIR')
    if override:
        path = Path(override).expanduser()
    else:
        path = Path.home() / '.geoport'
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        return Path.cwd()
def get_pmd3_dir() -> Path:
    """Return primary ~/.pymobiledevice3 pair-record directory."""
    override = os.getenv('GEOPORT_PAIR_RECORDS_DIR')
    if override:
        path = Path(override).expanduser()
    else:
        path = Path.home() / '.pymobiledevice3'
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path
def get_bonjour_timeout() -> float:
    """Return mDNS/Bonjour browse timeout in seconds."""
    default_val = 4.0 if sys.platform.startswith('linux') else 3.0
    try:
        return max(1.0, float(os.getenv('GEOPORT_BONJOUR_TIMEOUT', str(default_val))))
    except ValueError:
        return default_val
def is_remotepairing_discovery_enabled() -> bool:
    """Whether _remotepairing._tcp active discovery is enabled during /list_devices.\n\nDefaults to False on Linux (where classic _apple-mobdev2._tcp Lockdown over TCP\nis faster and works with copied lockdown pair records) and True on macOS/Windows.\nNote: RemotePairing tunnel fallback during /connect_device remains available everywhere.\n"""
    raw = os.getenv('GEOPORT_ENABLE_WIFI_DISCOVERY_REMOTEPAIRING', '').strip()
    if sys.platform.startswith('linux') and raw == '':
        return False
    else:
        return raw.lower() not in ['0', 'false', 'no', 'off']
WIFI_DISCOVERY_CACHE_TTL = float(os.getenv('GEOPORT_WIFI_DISCOVERY_CACHE_TTL', '10'))
MOBDEV2_CACHE_TTL = float(os.getenv('GEOPORT_MOBDEV2_CACHE_TTL', '5'))
LOCATION_KEEPALIVE_INTERVAL = float(os.getenv('GEOPORT_LOCATION_KEEPALIVE_SECONDS', '15'))
def configure_logging(level: int | None=None) -> logging.Logger:
    """Configure application logging cleanly without noisy werkzeug request spam."""
    if level is None:
        env_level = os.getenv('GEOPORT_LOG_LEVEL', 'INFO').upper()
        level = getattr(logging, env_level, logging.INFO)
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(level=level, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
    app_logger = logging.getLogger('GeoPort')
    app_logger.setLevel(level)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    logging.getLogger('zeroconf').setLevel(logging.WARNING)
    return app_logger
logger = configure_logging()