# Hand-repaired from GeoPort v5.0.3 bytecode (see docs/RECOVERY.md).
"""Australian Fuel Mode service (projectzerothree.info) with thread-safe TTL caching."""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

import requests

from geoport.config import FUEL_API_URL, FUEL_CACHE_TTL_SECONDS, logger


class FuelService:
    """Fetches and caches regional fuel price data without blocking page renders."""

    def __init__(self, api_url: str = FUEL_API_URL, cache_ttl: float = FUEL_CACHE_TTL_SECONDS) -> None:
        self._api_url = api_url
        self._cache_ttl = cache_ttl
        self._lock = threading.Lock()
        self._cached_data = None
        self._cached_at = 0.0

    def fetch_data(self, force: bool = False) -> Optional[dict[str, Any]]:
        now = time.time()
        with self._lock:
            if not force and self._cached_data is not None and now - self._cached_at < self._cache_ttl:
                return self._cached_data
        try:
            resp = requests.get(
                self._api_url,
                headers={'User-Agent': 'GeoPort/5.0'},
                verify=False,
                timeout=5.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, dict) and 'regions' in payload:
                with self._lock:
                    self._cached_data = payload
                    self._cached_at = time.time()
                return payload
        except Exception as exc:
            logger.warning(f'Fuel API fetch failed (returning cached data if available): {exc}')
        with self._lock:
            return self._cached_data

    def warm_async(self) -> None:
        """Warm the fuel cache in a background daemon thread so `/` renders instantaneously."""
        threading.Thread(target=self.fetch_data, daemon=True, name='geoport-fuel-warm').start()

    def get_region_prices(self, region: str = 'QLD') -> list[dict[str, Any]]:
        data = self.fetch_data()
        if not data:
            return []
        for entry in data.get('regions') or []:
            if entry.get('region') == region:
                return list(entry.get('prices') or [])
        return []

    def get_fuel_types(self, region: str = 'QLD') -> Optional[list[str]]:
        data = self.fetch_data()
        if data is None:
            return None
        prices = self.get_region_prices(region=region)
        seen = []
        for item in prices:
            ft = item.get('type')
            if not isinstance(ft, str):
                continue
            if ft not in seen:
                seen.append(ft)
        return sorted(seen, key=lambda x: (0, x) if x == 'U98' else (1, x))

    def get_price_data(self, fuel_type: str, region: str = 'QLD') -> tuple[Optional[dict[str, Any]], bool]:
        """Return `(selected_price_dict, api_available)`."""
        data = self.fetch_data()
        if data is None:
            return (None, False)
        prices = self.get_region_prices(region=region)
        selected = next((entry for entry in prices if entry.get('type') == fuel_type), None)
        return (selected, True)
