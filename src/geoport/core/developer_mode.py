# Hand-repaired from GeoPort v5.0.3 bytecode (see docs/RECOVERY.md).
"""Developer Mode inspection and activation (AmfiService) for USB and Wi-Fi devices."""
from __future__ import annotations

from typing import Any, Optional

from pymobiledevice3.exceptions import DeviceHasPasscodeSetError
from pymobiledevice3.lockdown import create_using_tcp, create_using_usbmux
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.amfi import AmfiService

from geoport.config import LOCKDOWN_TCP_PORT, logger
from geoport.core.pair_records import get_local_pair_record


async def check_developer_mode(
    udid: Optional[str],
    wifi_host: Optional[str] = None,
    rsd: Optional[RemoteServiceDiscoveryService] = None,
) -> Optional[bool]:
    """Return Developer Mode status (`True`/`False`), or `None` if no Lockdown/RSD transport is reachable yet."""
    if rsd is not None:
        try:
            status = await rsd.get_developer_mode_status()
            logger.info(f'Developer Mode status via RSD ({udid}): {status}')
            return bool(status)
        except Exception as exc:
            logger.debug(f'RSD Developer Mode check failed: {exc}')
    if udid:
        ld = None
        try:
            ld = await create_using_usbmux(serial=udid, connection_type='USB', autopair=True)
            status = await ld.get_developer_mode_status()
            logger.info(f'Developer Mode status via USB ({udid}): {status}')
            return bool(status)
        except Exception as exc:
            logger.debug(f'USB Developer Mode check unavailable for {udid}: {exc}')
        finally:
            if ld is not None:
                try:
                    await ld.close()
                except Exception:
                    pass
    if wifi_host and udid:
        pair_record = get_local_pair_record(udid)
        ld = None
        try:
            ld = await create_using_tcp(
                hostname=wifi_host,
                port=LOCKDOWN_TCP_PORT,
                identifier=udid,
                autopair=False,
                pair_record=pair_record,
                keep_alive=False,
            )
            status = await ld.get_developer_mode_status()
            logger.info(f'Developer Mode status via TCP {wifi_host} ({udid}): {status}')
            return bool(status)
        except Exception as exc:
            logger.debug(f'Wi-Fi TCP Developer Mode check failed for {wifi_host}: {exc}')
        finally:
            if ld is not None:
                try:
                    await ld.close()
                except Exception:
                    pass
    return None


async def enable_developer_mode(
    udid: Optional[str],
    wifi_host: Optional[str] = None,
) -> tuple[bool, Optional[str], dict[str, Any]]:
    """Enable Developer Mode via AmfiService over USB or Wi-Fi Lockdown.

If a device passcode is set (`DeviceHasPasscodeSetError`), automatically invokes
`AmfiService.reveal_developer_mode_option_in_ui()` (`action=0`) which unhides
Settings -> Privacy & Security -> Developer Mode WITHOUT requiring passcode removal.
"""
    if not udid:
        return (False, 'No device UDID provided.', {})
    ld = None
    try:
        try:
            ld = await create_using_usbmux(serial=udid, autopair=True)
        except Exception as usb_exc:
            if not wifi_host:
                return (
                    False,
                    f'Could not reach device over USB ({usb_exc}) and no Wi-Fi host is available. Connect via USB first.',
                    {},
                )
            pair_record = get_local_pair_record(udid)
            if pair_record is None:
                return (
                    False,
                    'No Pair Record found for Wi-Fi Developer Mode activation. Connect once over USB first.',
                    {},
                )
            ld = await create_using_tcp(
                hostname=wifi_host,
                port=LOCKDOWN_TCP_PORT,
                identifier=udid,
                autopair=False,
                pair_record=pair_record,
                keep_alive=True,
            )
        amfi = AmfiService(ld)
        try:
            await amfi.enable_developer_mode(enable_post_restart=True)
            return (True, None, {'revealed_only': False, 'udid': udid})
        except DeviceHasPasscodeSetError:
            revealed = False
            try:
                await amfi.reveal_developer_mode_option_in_ui()
                revealed = True
            except Exception as rev_exc:
                logger.warning(f'Failed to reveal Developer Mode menu in Settings: {rev_exc}')
            if revealed:
                msg = (
                    "Device has a passcode set, so GeoPort revealed the Developer Mode toggle directly on your iPhone!\n\n"
                    "On your iPhone:\n"
                    "1. Open Settings -> Privacy & Security -> Developer Mode\n"
                    "2. Toggle Developer Mode ON and tap Restart\n"
                    "3. After reboot, tap 'Turn On' and enter your passcode.\n\n"
                    "(You do NOT need to remove Face ID or your passcode.)"
                )
                return (False, msg, {'revealed_only': True, 'udid': udid})
            msg = (
                'Error: Device has a passcode set.\n\n'
                'Either toggle Settings -> Privacy & Security -> Developer Mode directly on your device, '
                'or temporarily remove your passcode under Settings -> Face ID & Passcode and try again.'
            )
            return (False, msg, {'revealed_only': False, 'udid': udid})
    except Exception as exc:
        logger.error(f'Enable Developer Mode failed for {udid}: {exc}')
        return (False, str(exc), {})
    finally:
        if ld is not None:
            try:
                await ld.close()
            except Exception:
                pass
