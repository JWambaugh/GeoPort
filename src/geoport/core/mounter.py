# Hand-repaired from GeoPort v5.0.3 bytecode (see docs/RECOVERY.md).
"""Developer Disk Image mounting helper (used for legacy iOS < 17 or explicit mount requests)."""
from __future__ import annotations

from typing import Optional

from pymobiledevice3.lockdown import LockdownClient, create_using_tcp, create_using_usbmux
from pymobiledevice3.services.mobile_image_mounter import auto_mount

from geoport.config import LOCKDOWN_TCP_PORT, logger
from geoport.core.pair_records import get_local_pair_record


async def mount_developer_image(
    udid: Optional[str],
    wifi_host: Optional[str] = None,
    lockdown_client: Optional[LockdownClient] = None,
) -> None:
    """Mount Developer Disk Image via `auto_mount`.

Note: On iOS 17+, `com.apple.instruments.dtservicehub` is built into iOS when
Developer Mode is enabled, so mounting a DDI is not required for LocationSimulation.
"""
    owns_client = False
    ld = lockdown_client
    try:
        if ld is None:
            owns_client = True
            try:
                ld = await create_using_usbmux(serial=udid, autopair=True)
            except Exception:
                if not wifi_host or not udid:
                    raise
                ld = await create_using_tcp(
                    hostname=wifi_host,
                    port=LOCKDOWN_TCP_PORT,
                    identifier=udid,
                    autopair=False,
                    pair_record=get_local_pair_record(udid),
                    keep_alive=True,
                )
        await auto_mount(ld)
        logger.info(f'Developer Disk Image mounted cleanly for {udid}')
    finally:
        if owns_client and ld is not None:
            try:
                await ld.close()
            except Exception:
                pass
