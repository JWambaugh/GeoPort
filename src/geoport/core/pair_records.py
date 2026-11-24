# Hand-repaired from GeoPort v5.0.3 bytecode (see docs/RECOVERY.md).
"""Pairing record management, headless usbmuxd safety patch, metadata cache, and IPA pairing export."""
from __future__ import annotations

import asyncio
import json
import os
import plistlib
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import pymobiledevice3.pair_records as _pmd3_pair_records
from pymobiledevice3.common import get_home_folder
from pymobiledevice3.exceptions import MuxException
import pymobiledevice3.lockdown as _pmd3_lockdown
from geoport.config import get_geoport_state_dir, get_pmd3_dir, logger

_patch_lock = threading.Lock()
_patched = False
_cache_lock = threading.Lock()


def install_headless_usbmux_patch() -> None:
    """Allow pymobiledevice3 pair-record lookups to succeed when usbmuxd is absent.

On headless Linux VMs/LXCs without a /var/run/usbmuxd socket (or Windows hosts with AMDS
stopped/firewalled), pymobiledevice3's `get_preferred_pair_record()` and `lockdown._known_lockdown_pair_records()`
await `get_usbmux_pairing_record()` first. Wrapping both module references asynchronously with a
short timeout converts missing-usbmuxd errors into `None` so local pair records work seamlessly.
"""
    global _patched
    with _patch_lock:
        if _patched:
            return
        orig = _pmd3_pair_records.get_usbmux_pairing_record

        async def _get_usbmux_pairing_record_safe(
            identifier: str,
            usbmux_address: Optional[str] = None,
        ) -> Optional[dict[str, Any]]:
            try:
                return await asyncio.wait_for(orig(identifier=identifier, usbmux_address=usbmux_address), timeout=2.5)
            except (FileNotFoundError, ConnectionRefusedError, MuxException, OSError, asyncio.TimeoutError, AttributeError):
                return None

        _pmd3_pair_records.get_usbmux_pairing_record = _get_usbmux_pairing_record_safe
        _pmd3_lockdown.get_usbmux_pairing_record = _get_usbmux_pairing_record_safe
        _patched = True


install_headless_usbmux_patch()


def get_pair_record_search_dirs() -> list[Path]:
    """Return ordered list of directories to search for lockdown & remote pairing records."""
    candidates = [get_pmd3_dir(), get_home_folder()]
    if sys.platform.startswith('linux'):
        candidates.append(Path('/var/lib/lockdown'))
    elif sys.platform == 'darwin':
        candidates.append(Path('/var/db/lockdown'))
    elif sys.platform == 'win32':
        program_data = os.environ.get('ALLUSERSPROFILE') or 'C:\\ProgramData'
        candidates.append(Path(program_data) / 'Apple' / 'Lockdown')
    unique = []
    seen = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _is_valid_udid_stem(stem: str) -> bool:
    return bool(stem) and len(stem) >= 16 and all(c.isalnum() or c == '-' for c in stem)


def iter_local_lockdown_pair_records() -> dict[str, dict[str, Any]]:
    """Return mapping of {udid: pair_record_dict} across all readable pair-record directories."""
    records = {}
    for folder in get_pair_record_search_dirs():
        try:
            if not folder.exists():
                continue
            files = sorted(folder.glob('*.plist'))
        except OSError:
            continue
        for file in files:
            name = file.name
            if name.startswith('._') or name == '.DS_Store' or name.startswith('remote_') or name == 'SystemConfiguration.plist':
                continue
            udid = name[:-6]
            if not _is_valid_udid_stem(udid) or udid in records:
                continue
            try:
                data = plistlib.loads(file.read_bytes())
                if isinstance(data, dict) and 'HostID' in data and 'HostCertificate' in data:
                    data.setdefault('UDID', udid)
                    records[udid] = data
            except (OSError, plistlib.InvalidFileException):
                continue
    return records


def list_local_paired_udids() -> list[str]:
    """Return sorted list of UDIDs that have a local Lockdown pair record."""
    return sorted(iter_local_lockdown_pair_records().keys())


def list_remote_paired_udids() -> list[str]:
    """Return sorted list of UDIDs that have a RemotePairing (`remote_<UDID>.plist`) record."""
    udids = set()
    for folder in get_pair_record_search_dirs():
        try:
            if not folder.exists():
                continue
            for file in folder.glob('remote_*.plist'):
                name = file.name
                if name.startswith('._'):
                    continue
                udid = name[len('remote_'):-6]
                if _is_valid_udid_stem(udid):
                    udids.add(udid)
        except OSError:
            continue
    return sorted(udids)


def get_local_pair_record(udid: Optional[str]) -> Optional[dict[str, Any]]:
    """Load `<UDID>.plist` Lockdown pair record from disk if available."""
    if not udid:
        return None
    for folder in get_pair_record_search_dirs():
        candidate = folder / f'{udid}.plist'
        try:
            if candidate.exists():
                data = plistlib.loads(candidate.read_bytes())
                if isinstance(data, dict) and 'HostID' in data:
                    data.setdefault('UDID', udid)
                    return data
        except (OSError, plistlib.InvalidFileException):
            continue
    return None


def has_remote_pair_record(udid: Optional[str]) -> bool:
    """Return True if `remote_<UDID>.plist` exists on disk."""
    if not udid:
        return False
    for folder in get_pair_record_search_dirs():
        candidate = folder / f'remote_{udid}.plist'
        try:
            if candidate.exists():
                return True
        except OSError:
            continue
    return False


def persist_pair_record_copy(udid: str, pair_record: dict[str, Any]) -> Optional[Path]:
    """Save a copy of a Lockdown pair record into ~/.pymobiledevice3/<UDID>.plist if not present."""
    if not udid or not isinstance(pair_record, dict) or 'HostID' not in pair_record:
        return None
    target_dir = get_pmd3_dir()
    target_path = target_dir / f'{udid}.plist'
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        tmp = target_path.with_suffix('.plist.tmp')
        tmp.write_bytes(plistlib.dumps(pair_record, fmt=plistlib.FMT_XML))
        os.replace(tmp, target_path)
        return target_path
    except OSError as exc:
        logger.debug(f'Could not persist local pair record copy to {target_path}: {exc}')
        return None


def get_remote_pair_record_bytes(udid: Optional[str]) -> Optional[bytes]:
    """Return normalized `remote_<UDID>.plist` RPPairing XML plist bytes if available on disk."""
    if not udid:
        return None
    for folder in get_pair_record_search_dirs():
        candidate = folder / f'remote_{udid}.plist'
        try:
            if candidate.exists():
                raw = candidate.read_bytes()
                try:
                    parsed = plistlib.loads(raw)
                    if isinstance(parsed, dict):
                        if 'identifier' not in parsed:
                            parsed['identifier'] = _pmd3_pair_records.generate_host_id()
                        return plistlib.dumps(parsed, fmt=plistlib.FMT_XML)
                except Exception:
                    pass
                return raw
        except OSError:
            continue
    return None


def _serialize_lockdown_pairing_bytes(udid: str, record: dict[str, Any]) -> tuple[bytes, str]:
    export_dict = {}
    standard_keys = ('DeviceCertificate', 'EscrowBag', 'HostCertificate', 'HostID', 'HostPrivateKey', 'RootCertificate', 'RootPrivateKey', 'SystemBUID', 'WiFiMACAddress')
    for key in standard_keys:
        if key in record:
            export_dict[key] = record[key]
    for key, val in record.items():
        if key not in export_dict and not key.startswith('_'):
            export_dict[key] = val
    if 'WiFiMACAddress' not in export_dict:
        export_dict['WiFiMACAddress'] = '00:00:00:00:00:00'
    export_dict.pop('UDID', None)
    xml_bytes = plistlib.dumps(export_dict, fmt=plistlib.FMT_XML)
    return (xml_bytes, f'{udid}.mobiledevicepairing')


def export_mobiledevicepairing_bytes(udid: str) -> tuple[bytes, str]:
    """Export `<UDID>.mobiledevicepairing` XML plist from local pair-record cache."""
    if not udid or not _is_valid_udid_stem(udid):
        raise ValueError(f'Invalid UDID: {udid!r}')
    record = get_local_pair_record(udid)
    if record is None:
        raise FileNotFoundError(f'No Lockdown pair record found for UDID {udid}')
    return _serialize_lockdown_pairing_bytes(udid, record)


async def export_pairing_bytes_async(udid: str, prefer_rppairing: bool = False) -> tuple[bytes, str]:
    """Export pairing file for standalone iOS IPA apps (supports both RPPairing and Lockdown formats).

Queries `usbmuxd` (`get_preferred_pair_record`) as an automatic fallback on macOS/Windows
when `/var/db/lockdown` has root-only permissions and `~/.pymobiledevice3/<UDID>.plist` hasn't
been cached yet.
"""
    if not udid or not _is_valid_udid_stem(udid):
        raise ValueError(f'Invalid UDID: {udid!r}')
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        lockdown = await create_using_usbmux(serial=udid, connection_type='USB', autopair=False)
        try:
            if not await lockdown.get_enable_wifi_connections():
                await lockdown.set_enable_wifi_connections(True)
                logger.info(f'Enabled wireless lockdown connections on {udid} for standalone IPA')
        finally:
            await lockdown.close()
    except Exception:
        pass
    if prefer_rppairing:
        rp_bytes = get_remote_pair_record_bytes(udid)
        if rp_bytes is not None:
            return (rp_bytes, f'remote_{udid}.plist')
    record = get_local_pair_record(udid)
    if record is None:
        try:
            record = await _pmd3_pair_records.get_preferred_pair_record(udid, get_home_folder())
            if isinstance(record, dict) and 'HostID' in record:
                persist_pair_record_copy(udid, record)
        except Exception:
            record = None
    if record is not None:
        return _serialize_lockdown_pairing_bytes(udid, record)
    rp_fallback = get_remote_pair_record_bytes(udid)
    if rp_fallback is not None:
        return (rp_fallback, f'remote_{udid}.plist')
    raise FileNotFoundError(f'No Lockdown or RPPairing record found for UDID {udid}')


def get_device_cache_path() -> Path:
    return get_geoport_state_dir() / 'device-cache.json'


def load_device_meta_cache() -> dict[str, dict[str, Any]]:
    path = get_device_cache_path()
    with _cache_lock:
        try:
            if not path.exists():
                return {}
            data = json.loads(path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning(f'Failed to load device metadata cache from {path}: {exc}')
            return {}


def save_device_meta_cache(cache: dict[str, dict[str, Any]]) -> None:
    path = get_device_cache_path()
    with _cache_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding='utf-8')
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning(f'Failed to save device metadata cache to {path}: {exc}')


def update_device_meta_cache(udid: str, info: dict[str, Any]) -> dict[str, Any]:
    """Merge non-empty device metadata fields for `udid` into ~/.geoport/device-cache.json."""
    if not udid:
        return {}
    cache = load_device_meta_cache()
    existing = dict(cache.get(udid) or {})
    for key in ('DeviceName', 'ProductVersion', 'ProductType', 'BuildVersion', 'DeviceClass', 'WiFiMACAddress'):
        val = info.get(key)
        if val not in (None, '', 'Unknown'):
            existing[key] = val
    existing['_updatedAt'] = time.time()
    cache[udid] = existing
    save_device_meta_cache(cache)
    return existing
