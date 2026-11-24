# Hand-repaired from GeoPort v5.0.3 bytecode (see docs/RECOVERY.md).
"""Multi-transport iOS device scanner (USB usbmux + Wi-Fi _apple-mobdev2._tcp + _remotepairing._tcp + Manual CLI)."""
from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import suppress
from dataclasses import dataclass, field
import time
from typing import Any, Callable, Optional

from pymobiledevice3 import bonjour, usbmux
from pymobiledevice3.lockdown import compute_mobdev2_auth_tag, create_using_tcp, create_using_usbmux
from pymobiledevice3.remote.tunnel_service import get_remote_pairing_tunnel_services
from geoport.config import LOCKDOWN_TCP_PORT, MOBDEV2_CACHE_TTL, WIFI_DISCOVERY_CACHE_TTL, get_bonjour_timeout, is_remotepairing_discovery_enabled, logger
from geoport.core.pair_records import get_local_pair_record, has_remote_pair_record, iter_local_lockdown_pair_records, load_device_meta_cache, persist_pair_record_copy, update_device_meta_cache

try:
    from zeroconf import ServiceBrowser, Zeroconf
except Exception:
    Zeroconf = None
    ServiceBrowser = None


@dataclass
class Mobdev2Advert:
    """Normalized _apple-mobdev2._tcp mDNS advertisement from pymobiledevice3 or zeroconf."""
    instance: str
    addresses: list[str] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
    srv_port: int = 32498


def _is_ipv4(addr: str) -> bool:
    return '.' in addr and ':' not in addr


def _is_link_local(addr: str) -> bool:
    lower = addr.lower()
    return lower.startswith('fe80:') or lower.startswith('169.254.')


def _sort_addresses_prefer_ipv4(addresses: list[str]) -> list[str]:
    """Order addresses with routable IPv4 first, then global IPv6, then link-local."""
    deduped = []
    for addr in addresses:
        if addr and addr not in deduped:
            deduped.append(addr)

    def _score(addr: str) -> tuple[int, str]:
        if _is_ipv4(addr) and not _is_link_local(addr):
            return (0, addr)
        if not _is_ipv4(addr) and not _is_link_local(addr):
            return (1, addr)
        if _is_ipv4(addr):
            return (2, addr)
        return (3, addr)

    return sorted(deduped, key=_score)


def match_pair_records_for_advert(advert: Mobdev2Advert, records_by_udid: dict[str, dict[str, Any]]) -> tuple[list[tuple[str, dict[str, Any]]], bool]:
    """Return `(matched_udid_record_pairs, cryptographically_or_mac_verified)`.

Checks:
1. Exact `WiFiMACAddress` prefix match (`<mac>@..._apple-mobdev2._tcp.local.`), when Private Wi-Fi Address is off.
2. Cryptographic `authTag` / `authTag#*` match (`compute_mobdev2_auth_tag(HostID, properties['identifier'])`),
   which works even when iOS Private Wi-Fi Address randomizes the advertised MAC!
"""
    if not records_by_udid:
        return ([], False)
    mac_prefix = advert.instance.split('@', 1)[0].strip().lower() if '@' in advert.instance else ''
    if mac_prefix:
        for udid, rec in records_by_udid.items():
            rec_mac = str(rec.get('WiFiMACAddress') or '').strip().lower()
            if rec_mac and rec_mac == mac_prefix:
                return ([(udid, rec)], True)
    service_id = advert.properties.get('identifier')
    auth_tags = set()
    for key, val in advert.properties.items():
        if key == 'authTag' or key.startswith('authTag#'):
            with suppress(binascii.Error, ValueError, TypeError):
                auth_tags.add(base64.b64decode(val))
    if service_id and auth_tags:
        matched = []
        for udid, rec in records_by_udid.items():
            host_id = rec.get('HostID')
            if isinstance(host_id, str) and compute_mobdev2_auth_tag(host_id, service_id) in auth_tags:
                matched.append((udid, rec))
        return (matched, bool(matched))
    return (list(records_by_udid.items()), False)


async def _browse_mobdev2_pmd3(timeout_s: float) -> list[Mobdev2Advert]:
    try:
        instances = await bonjour.browse_mobdev2(timeout=timeout_s)
    except Exception as exc:
        logger.debug(f'pymobiledevice3 browse_mobdev2 failed: {exc}')
        return []
    out = []
    for item in instances:
        addrs = [a.full_ip for a in item.addresses or [] if getattr(a, 'full_ip', None)]
        props = {str(k): str(v) for k, v in (item.properties or {}).items() if v is not None}
        out.append(Mobdev2Advert(instance=item.instance, addresses=_sort_addresses_prefer_ipv4(addrs), properties=props, srv_port=int(item.port or 32498)))
    return out


def _browse_zeroconf_sync(service_type: str, timeout_s: float) -> list[Mobdev2Advert]:
    if Zeroconf is None or ServiceBrowser is None:
        return []
    found = []

    class _Listener:
        def add_service(self, zc: Any, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name)
            if not info:
                return
            addrs = [a for a in info.parsed_addresses() or [] if isinstance(a, str)]
            props = {}
            for k, v in (info.properties or {}).items():
                kk = k.decode('utf-8', errors='ignore') if isinstance(k, (bytes, bytearray)) else str(k)
                if v is None:
                    continue
                vv = v.decode('utf-8', errors='ignore') if isinstance(v, (bytes, bytearray)) else str(v)
                props[kk] = vv
            found.append(Mobdev2Advert(instance=name, addresses=_sort_addresses_prefer_ipv4(addrs), properties=props, srv_port=int(info.port or 32498)))

        def update_service(self, zc: Any, type_: str, name: str) -> None:
            pass

        def remove_service(self, zc: Any, type_: str, name: str) -> None:
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, service_type, _Listener())
        time.sleep(timeout_s)
    except Exception as exc:
        logger.debug(f'zeroconf browse ({service_type}) failed: {exc}')
    finally:
        with suppress(Exception):
            zc.close()
    return found


def _merge_mobdev2_adverts(*lists: list[Mobdev2Advert]) -> list[Mobdev2Advert]:
    merged_by_instance = {}
    for items in lists:
        for adv in items:
            existing = merged_by_instance.get(adv.instance)
            if existing is None:
                merged_by_instance[adv.instance] = Mobdev2Advert(instance=adv.instance, addresses=list(adv.addresses), properties=dict(adv.properties), srv_port=adv.srv_port)
            else:
                existing.addresses = _sort_addresses_prefer_ipv4(existing.addresses + adv.addresses)
                for k, v in adv.properties.items():
                    existing.properties.setdefault(k, v)
    return list(merged_by_instance.values())


class DeviceScanner:
    """Discovers iOS devices over USB, Wi-Fi Lockdown (_apple-mobdev2._tcp), RemotePairing, and CLI Manual Wi-Fi."""

    def __init__(self, locale_provider: Optional[Callable[[], Optional[str]]] = None, manual_wifihost: Optional[str] = None, manual_udid: Optional[str] = None) -> None:
        self._locale_provider = locale_provider or (lambda: None)
        self._manual_wifihost = manual_wifihost
        self._manual_udid = manual_udid
        self._mobdev2_cache = []
        self._mobdev2_cache_at = 0.0
        self._rp_cache = []
        self._rp_cache_at = 0.0
        self._last_wifi_endpoints_by_udid = {}

    def get_cached_wifi_endpoints(self, udid: Optional[str]) -> list[dict[str, Any]]:
        if not udid:
            return []
        return list(self._last_wifi_endpoints_by_udid.get(udid) or [])

    async def browse_mobdev2_adverts(self, force_refresh: bool = False) -> list[Mobdev2Advert]:
        now = time.time()
        if not force_refresh and self._mobdev2_cache and now - self._mobdev2_cache_at <= MOBDEV2_CACHE_TTL:
            return list(self._mobdev2_cache)
        timeout_s = min(get_bonjour_timeout(), 2.5)
        pmd_task = asyncio.create_task(_browse_mobdev2_pmd3(timeout_s))
        zc_task = asyncio.to_thread(_browse_zeroconf_sync, '_apple-mobdev2._tcp.local.', timeout_s)
        pmd_res, zc_res = await asyncio.gather(pmd_task, zc_task, return_exceptions=True)
        merged = _merge_mobdev2_adverts(pmd_res if isinstance(pmd_res, list) else [], zc_res if isinstance(zc_res, list) else [])
        if not merged:
            slow_timeout = max(3.5, get_bonjour_timeout())
            pmd_slow, zc_slow = await asyncio.gather(_browse_mobdev2_pmd3(slow_timeout), asyncio.to_thread(_browse_zeroconf_sync, '_apple-mobdev2._tcp.local.', slow_timeout), return_exceptions=True)
            merged = _merge_mobdev2_adverts(pmd_slow if isinstance(pmd_slow, list) else [], zc_slow if isinstance(zc_slow, list) else [])
        self._mobdev2_cache = merged
        self._mobdev2_cache_at = time.time()
        return list(merged)

    async def discover_devices(self, active_session_udid: Optional[str] = None) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """Return device map matching GeoPort UI contract: `{udid: {'USB': [info], 'Wifi': [info]}}`."""
        connected_devices = {}
        user_country = self._locale_provider()
        meta_cache = load_device_meta_cache()
        if self._manual_wifihost:
            await self._discover_manual_wifi(connected_devices, user_country)
        await self._discover_usbmux_devices(connected_devices, user_country)
        meta_cache = load_device_meta_cache()
        await self._discover_mobdev2_wifi_devices(connected_devices=connected_devices, meta_cache=meta_cache, user_country=user_country, active_session_udid=active_session_udid)
        if is_remotepairing_discovery_enabled():
            await self._discover_remotepairing_devices(connected_devices=connected_devices, meta_cache=load_device_meta_cache(), user_country=user_country)
        self._consolidate_wifi_entries(connected_devices)
        for udid, buckets in connected_devices.items():
            for bucket_name in ('Wifi', 'Manual Wifi'):
                for entry in buckets.get(bucket_name) or []:
                    eps = entry.get('wifiEndpoints') or []
                    if not eps and entry.get('wifiAddress'):
                        eps = [{'wifiAddress': entry['wifiAddress'], 'wifiPort': entry.get('wifiPort') or LOCKDOWN_TCP_PORT, 'discovery': entry.get('discovery') or 'mobdev2'}]
                    if eps:
                        self._last_wifi_endpoints_by_udid[udid] = eps
        return connected_devices

    async def _discover_manual_wifi(self, connected_devices: dict[str, dict[str, list[dict[str, Any]]]], user_country: Optional[str]) -> None:
        host = self._manual_wifihost
        if not host:
            return
        udid = self._manual_udid
        pair_record = get_local_pair_record(udid) if udid else None
        ld = None
        try:
            ld = await create_using_tcp(hostname=host, port=LOCKDOWN_TCP_PORT, identifier=udid, autopair=False, pair_record=pair_record, keep_alive=False)
            resolved_udid = ld.udid or udid
            if not resolved_udid:
                return
            info = dict(ld.short_info or {})
            update_device_meta_cache(resolved_udid, info)
            entry = self._build_device_entry(udid=resolved_udid, raw_info=info, conn_type='Network', wifi_state=True, user_country=user_country, wifi_address=host, wifi_port=LOCKDOWN_TCP_PORT, discovery='manual_wifi')
            connected_devices.setdefault(resolved_udid, {}).setdefault('Manual Wifi', []).append(entry)
        except Exception as exc:
            logger.warning(f'Manual Wi-Fi discovery to {host} failed: {exc}')
        finally:
            if ld is not None:
                with suppress(Exception):
                    await ld.close()

    async def _discover_usbmux_devices(self, connected_devices: dict[str, dict[str, list[dict[str, Any]]]], user_country: Optional[str]) -> None:
        try:
            mux_devices = await usbmux.list_devices()
        except FileNotFoundError:
            logger.debug('usbmuxd socket not present; skipping USB discovery (headless Wi-Fi mode).')
            return
        except Exception as exc:
            logger.debug(f'usbmux discovery unavailable: {exc}')
            return
        for mux_dev in mux_devices:
            udid = mux_dev.serial
            mux_conn_type = mux_dev.connection_type
            ld = None
            try:
                ld = await create_using_usbmux(serial=udid, connection_type=mux_conn_type, autopair=mux_conn_type == 'USB')
                info = dict(ld.short_info or {})
                if getattr(ld, 'pair_record', None):
                    persist_pair_record_copy(udid, ld.pair_record)
                wifi_state = False
                with suppress(Exception):
                    wifi_state = bool(await ld.get_enable_wifi_connections())
                    if not wifi_state and mux_conn_type == 'USB':
                        logger.info(f'Auto-enabling Wi-Fi connections on USB device {udid}')
                        await ld.set_enable_wifi_connections(True)
                        wifi_state = True
                update_device_meta_cache(udid, info)
                ui_bucket = 'Wifi' if mux_conn_type == 'Network' else 'USB'
                entry = self._build_device_entry(udid=udid, raw_info=info, conn_type='Network' if mux_conn_type == 'Network' else 'USB', wifi_state=wifi_state, user_country=user_country, discovery='usbmux_network' if mux_conn_type == 'Network' else 'usbmux_usb')
                connected_devices.setdefault(udid, {}).setdefault(ui_bucket, []).append(entry)
            except Exception as exc:
                logger.warning(f'Failed inspecting usbmux device {udid} ({mux_conn_type}): {exc}')
            finally:
                if ld is not None:
                    with suppress(Exception):
                        await ld.close()

    async def _discover_mobdev2_wifi_devices(self, connected_devices: dict[str, dict[str, list[dict[str, Any]]]], meta_cache: dict[str, dict[str, Any]], user_country: Optional[str], active_session_udid: Optional[str] = None) -> None:
        records_by_udid = iter_local_lockdown_pair_records()
        if not records_by_udid:
            return
        adverts = await self.browse_mobdev2_adverts()
        if not adverts:
            return
        logger.info(f'Discovered {len(adverts)} _apple-mobdev2._tcp advert(s); matching against {len(records_by_udid)} local pair record(s)')
        for adv in adverts:
            if not adv.addresses:
                continue
            candidates, verified_match = match_pair_records_for_advert(adv, records_by_udid)
            if not candidates:
                continue
            endpoints = [{'wifiAddress': addr, 'wifiPort': LOCKDOWN_TCP_PORT, 'discovery': 'mobdev2'} for addr in adv.addresses]
            primary_addr = adv.addresses[0]
            if verified_match and len(candidates) == 1 and candidates[0][0] == active_session_udid:
                matched_udid = candidates[0][0]
                cached_info = dict(meta_cache.get(matched_udid) or {})
                entry = self._build_device_entry(udid=matched_udid, raw_info=cached_info, conn_type='Network', wifi_state=True, user_country=user_country, wifi_address=primary_addr, wifi_port=LOCKDOWN_TCP_PORT, wifi_endpoints=endpoints, discovery='mobdev2')
                connected_devices.setdefault(matched_udid, {}).setdefault('Wifi', []).append(entry)
                continue
            probed_ok = False
            for addr in adv.addresses:
                for cand_udid, cand_record in candidates:
                    ld = None
                    try:
                        ld = await asyncio.wait_for(create_using_tcp(hostname=addr, port=LOCKDOWN_TCP_PORT, identifier=cand_udid, autopair=False, pair_record=cand_record, keep_alive=False), timeout=4.0)
                        if not ld.paired:
                            continue
                        resolved_udid = ld.udid or cand_udid
                        if resolved_udid != cand_udid:
                            continue
                        info = dict(ld.short_info or {})
                        update_device_meta_cache(resolved_udid, info)
                        entry = self._build_device_entry(udid=resolved_udid, raw_info=info, conn_type='Network', wifi_state=True, user_country=user_country, wifi_address=addr, wifi_port=LOCKDOWN_TCP_PORT, wifi_endpoints=endpoints, discovery='mobdev2')
                        connected_devices.setdefault(resolved_udid, {}).setdefault('Wifi', []).append(entry)
                        probed_ok = True
                        break
                    except Exception as exc:
                        logger.debug(f'mobdev2 TCP 62078 probe to {addr} for {cand_udid} failed: {exc}')
                    finally:
                        if ld is not None:
                            with suppress(Exception):
                                await ld.close()
                if probed_ok:
                    break
            if not probed_ok and verified_match and len(candidates) == 1:
                matched_udid = candidates[0][0]
                cached_info = dict(meta_cache.get(matched_udid) or {})
                entry = self._build_device_entry(udid=matched_udid, raw_info=cached_info, conn_type='Network', wifi_state=True, user_country=user_country, wifi_address=primary_addr, wifi_port=LOCKDOWN_TCP_PORT, wifi_endpoints=endpoints, discovery='mobdev2')
                connected_devices.setdefault(matched_udid, {}).setdefault('Wifi', []).append(entry)

    async def _discover_remotepairing_devices(self, connected_devices: dict[str, dict[str, list[dict[str, Any]]]], meta_cache: dict[str, dict[str, Any]], user_country: Optional[str]) -> None:
        now = time.time()
        if now - self._rp_cache_at > WIFI_DISCOVERY_CACHE_TTL:
            discovered = []
            try:
                services = await get_remote_pairing_tunnel_services(timeout=get_bonjour_timeout())
                for svc in services:
                    try:
                        discovered.append({'udid': getattr(svc, 'remote_identifier', None), 'hostname': getattr(svc, 'hostname', None), 'port': getattr(svc, 'port', None)})
                    finally:
                        with suppress(Exception):
                            await svc.close()
                self._rp_cache = discovered
                self._rp_cache_at = now
            except Exception as exc:
                logger.debug(f'RemotePairing discovery failed (non-fatal): {exc}')
        grouped = {}
        for item in self._rp_cache:
            rp_udid = item.get('udid')
            rp_host = item.get('hostname')
            rp_port = item.get('port')
            if rp_udid and rp_host and rp_port:
                grouped.setdefault(rp_udid, []).append({'wifiAddress': rp_host, 'wifiPort': rp_port, 'discovery': 'remote_pairing'})
        for rp_udid, endpoints in grouped.items():
            existing_wifi = connected_devices.get(rp_udid, {}).get('Wifi') or []
            if existing_wifi:
                existing_wifi[0]['wifiEndpoints'] = list(existing_wifi[0].get('wifiEndpoints') or []) + endpoints
                continue
            usb_info = (connected_devices.get(rp_udid, {}).get('USB') or [None])[0]
            raw_info = dict(usb_info or meta_cache.get(rp_udid) or {})
            primary = next((ep for ep in endpoints if not _is_link_local(str(ep['wifiAddress']))), endpoints[0])
            entry = self._build_device_entry(udid=rp_udid, raw_info=raw_info, conn_type='Network', wifi_state=True, user_country=user_country, wifi_address=primary['wifiAddress'], wifi_port=primary['wifiPort'], wifi_endpoints=endpoints, discovery='remote_pairing')
            connected_devices.setdefault(rp_udid, {}).setdefault('Wifi', []).append(entry)

    @staticmethod
    def _consolidate_wifi_entries(connected_devices: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
        for udid, buckets in connected_devices.items():
            wifi_list = buckets.get('Wifi') or []
            if len(wifi_list) <= 1:
                continue
            priority = {'mobdev2': 0, 'remote_pairing': 1, 'usbmux_network': 2}
            wifi_list_sorted = sorted(wifi_list, key=lambda d: priority.get(str(d.get('discovery')), 9))
            best = dict(wifi_list_sorted[0])
            merged_eps = []
            seen_ep = set()
            for item in wifi_list_sorted:
                for key in ('DeviceName', 'ProductVersion', 'ProductType', 'BuildVersion', 'DeviceClass'):
                    if best.get(key) in (None, '', 'Unknown') and item.get(key) not in (None, '', 'Unknown'):
                        best[key] = item[key]
                for ep in item.get('wifiEndpoints') or []:
                    sig = (ep.get('wifiAddress'), ep.get('wifiPort'))
                    if sig not in seen_ep:
                        seen_ep.add(sig)
                        merged_eps.append(ep)
            if merged_eps:
                best['wifiEndpoints'] = merged_eps
            buckets['Wifi'] = [best]

    @staticmethod
    def _build_device_entry(udid: str, raw_info: dict[str, Any], conn_type: str, wifi_state: bool, user_country: Optional[str], wifi_address: Optional[str] = None, wifi_port: Optional[int] = None, wifi_endpoints: Optional[list[dict[str, Any]]] = None, discovery: str = 'mobdev2') -> dict[str, Any]:
        product_type = raw_info.get('ProductType') or ''
        inferred_class = 'iPad' if 'ipad' in str(product_type).lower() else 'iPhone'
        device_class = raw_info.get('DeviceClass') or inferred_class
        device_name = raw_info.get('DeviceName') or device_class
        product_version = raw_info.get('ProductVersion') or '17.0'
        eps = list(wifi_endpoints or [])
        if not eps and wifi_address:
            eps = [{'wifiAddress': wifi_address, 'wifiPort': wifi_port or LOCKDOWN_TCP_PORT, 'discovery': discovery}]
        return {'Identifier': udid, 'UniqueDeviceID': udid, 'DeviceName': device_name, 'ProductVersion': product_version, 'ProductType': product_type, 'BuildVersion': raw_info.get('BuildVersion'), 'DeviceClass': device_class, 'ConnectionType': conn_type, 'wifiState': bool(wifi_state), 'wifiAddress': wifi_address, 'wifiPort': wifi_port, 'wifiEndpoints': eps, 'userLocale': user_country, 'discovery': discovery, 'hasPairRecord': bool(get_local_pair_record(udid) or has_remote_pair_record(udid))}
