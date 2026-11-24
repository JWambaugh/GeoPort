# Hand-repaired from GeoPort v5.0.3 bytecode (see docs/RECOVERY.md).
"""Cached GitHub version/broadcast lookup and user country resolution."""
from __future__ import annotations

import json
import locale
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Optional

import pycountry
import requests

from geoport.config import (
    APP_VERSION_NUMBER,
    BASE_DIR,
    BROADCAST_FILE,
    CURRENT_VERSION_FILE,
    GITHUB_REPO,
    MAP_LAYERS_FILE,
    get_geoport_state_dir,
    logger,
)


def _parse_semver_tuple(ver: str) -> tuple[int, ...]:
    nums = re.findall('\\d+', ver or '')
    return tuple(int(n) for n in nums) if nums else (0,)


def format_broadcast_html(raw_text: str) -> Optional[str]:
    text = (raw_text or '').strip()
    if not text:
        return None
    url_match = re.search('(https?://\\S+)', text)
    if not url_match:
        return text
    url = url_match.group(1)
    target = ' target="_blank" rel="noopener noreferrer"' if url.startswith('https://') else ''
    return text[:url_match.start()] + f'<a href="{url}"{target}>{url}</a>' + text[url_match.end():]


class MetadataService:
    """Caches GitHub update metadata and user country so page loads and device scans stay fast."""

    def __init__(self, ttl_seconds: float = 1800.0, map_ttl_seconds: float = 600.0) -> None:
        self._ttl = ttl_seconds
        self._map_ttl = map_ttl_seconds
        self._lock = threading.Lock()
        self._country = None
        self._github_version = None
        self._github_broadcast = None
        self._fetched_at = 0.0
        self._remote_map_layers = None
        self._map_fetched_at = 0.0

    def get_user_country(self) -> str:
        with self._lock:
            if self._country:
                return self._country
        resolved = self._resolve_country()
        with self._lock:
            self._country = resolved
        return resolved

    @staticmethod
    def _resolve_country() -> str:
        try:
            loc, _ = locale.getlocale()
            if not loc:
                loc, _ = locale.getdefaultlocale()
            if loc and '_' in loc:
                alpha2 = loc.split('_')[-1].split('.')[0].upper()
                country = pycountry.countries.get(alpha_2=alpha2)
                if country and getattr(country, 'name', None):
                    return str(country.name)
        except Exception:
            pass
        try:
            resp = requests.get('http://ip-api.com/json/', timeout=2.5)
            if resp.status_code == 200:
                name = resp.json().get('country')
                if isinstance(name, str) and name:
                    return name
        except Exception:
            return 'Australia'
        return 'Australia'

    def refresh_github_metadata(self) -> None:
        version_val = None
        broadcast_val = None
        try:
            url_v = f'https://raw.githubusercontent.com/{GITHUB_REPO}/main/{CURRENT_VERSION_FILE}'
            resp_v = requests.get(url_v, verify=False, timeout=3.0)
            if resp_v.ok:
                version_val = resp_v.text.strip()
        except Exception as exc:
            logger.debug(f'GitHub version check skipped: {exc}')
        try:
            url_b = f'https://raw.githubusercontent.com/{GITHUB_REPO}/main/{BROADCAST_FILE}'
            resp_b = requests.get(url_b, verify=False, timeout=3.0)
            if resp_b.ok:
                broadcast_val = format_broadcast_html(resp_b.text)
        except Exception as exc:
            logger.debug(f'GitHub broadcast check skipped: {exc}')
        with self._lock:
            self._github_version = version_val
            self._github_broadcast = broadcast_val
            self._fetched_at = time.time()

    def get_version_and_broadcast(self) -> tuple[Optional[str], Optional[str]]:
        now = time.time()
        with self._lock:
            stale = now - self._fetched_at > self._ttl
            gh_ver = self._github_version
            gh_bcast = self._github_broadcast
        if stale:
            if self._fetched_at == 0.0:
                self.refresh_github_metadata()
                with self._lock:
                    gh_ver = self._github_version
                    gh_bcast = self._github_broadcast
            else:
                threading.Thread(target=self.refresh_github_metadata, daemon=True).start()
        version_message = None
        if gh_ver:
            remote_t = _parse_semver_tuple(gh_ver)
            local_t = _parse_semver_tuple(APP_VERSION_NUMBER)
            if remote_t > local_t:
                version_message = f'Update available. New Version is {gh_ver}'
                return (version_message, gh_bcast)
            if remote_t < local_t:
                version_message = f'GeoPort v{APP_VERSION_NUMBER} (Userspace RSD Edition)'
        return (version_message, gh_bcast)

    @staticmethod
    def _load_json_file(path: Path) -> Optional[dict[str, Any]]:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, dict) and isinstance(data.get('layers'), list):
                return data
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.warning(f'Ignoring invalid JSON in {path}: {exc}')
            return None
        return None

    def refresh_remote_map_layers(self) -> Optional[dict[str, Any]]:
        """Fetch live `MAP_LAYERS.json` from GitHub so map overlays can be updated without rebuilding binaries."""
        url = os.getenv(
            'GEOPORT_MAP_LAYERS_URL',
            f'https://raw.githubusercontent.com/{GITHUB_REPO}/main/{MAP_LAYERS_FILE}',
        )
        try:
            resp = requests.get(url, verify=False, timeout=3.5)
            if resp.ok:
                parsed = resp.json()
                if isinstance(parsed, dict) and isinstance(parsed.get('layers'), list) and parsed['layers']:
                    with self._lock:
                        self._remote_map_layers = parsed
                        self._map_fetched_at = time.time()
                    try:
                        cache_path = get_geoport_state_dir() / 'remote_map_layers_cache.json'
                        cache_path.write_text(json.dumps(parsed, indent=2), encoding='utf-8')
                    except OSError:
                        pass
                    return parsed
        except Exception as exc:
            logger.debug(f'Remote MAP_LAYERS.json fetch skipped: {exc}')
        with self._lock:
            self._map_fetched_at = time.time()
        return None

    def get_map_layers_config(self, force_refresh: bool = False) -> dict[str, Any]:
        """Return merged map layers configuration (local override -> live GitHub -> cached remote -> bundled)."""
        bundled = self._load_json_file(BASE_DIR / 'static' / 'map_layers.json') or {'defaults': {}, 'layers': []}
        state_dir = get_geoport_state_dir()
        now = time.time()
        with self._lock:
            remote_cfg = self._remote_map_layers
            stale = now - self._map_fetched_at > self._map_ttl
        if force_refresh:
            remote_cfg = self.refresh_remote_map_layers() or remote_cfg
        elif stale:
            if self._map_fetched_at == 0.0:
                cached = self._load_json_file(state_dir / 'remote_map_layers_cache.json')
                if cached and cached.get('layers'):
                    with self._lock:
                        self._remote_map_layers = cached
                    remote_cfg = cached
                    threading.Thread(target=self.refresh_remote_map_layers, daemon=True).start()
                else:
                    remote_cfg = self.refresh_remote_map_layers()
            else:
                threading.Thread(target=self.refresh_remote_map_layers, daemon=True).start()
        active = dict(remote_cfg) if remote_cfg and remote_cfg.get('layers') else dict(bundled)
        source = 'github_live' if remote_cfg and remote_cfg.get('layers') else 'bundled'
        for local_candidate in (Path.cwd() / 'MAP_LAYERS.json', state_dir / 'map_layers.json'):
            local_cfg = self._load_json_file(local_candidate)
            if not local_cfg:
                continue
            if isinstance(local_cfg.get('layers'), list) and local_cfg['layers']:
                active['layers'] = local_cfg['layers']
                source = f'local:{local_candidate}'
            elif isinstance(local_cfg.get('extra_layers'), list):
                active['layers'] = list(local_cfg['extra_layers']) + list(active.get('layers', []))
                source = f'local_merged:{local_candidate}'
            if isinstance(local_cfg.get('defaults'), dict):
                merged_defaults = dict(active.get('defaults') or {})
                merged_defaults.update(local_cfg['defaults'])
                active['defaults'] = merged_defaults
            if isinstance(local_cfg.get('stadia_api_key'), str) and local_cfg['stadia_api_key'].strip():
                active['stadia_api_key'] = local_cfg['stadia_api_key'].strip()
        env_stadia_key = os.getenv('GEOPORT_STADIA_API_KEY', '').strip()
        if env_stadia_key and not active.get('stadia_api_key'):
            active['stadia_api_key'] = env_stadia_key
        active['source'] = source
        return active
