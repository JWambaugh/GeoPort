# Hand-repaired from GeoPort v5.0.3 bytecode (see docs/RECOVERY.md).
"""Unified rootless Userspace RSD Tunnel + persistent DVT LocationSimulation session manager.

Key Guarantees:
1. Rootless Userspace RSD Tunnels on Linux, macOS, and Windows via pymobiledevice3 11.61.0:
   - USB: `PreferredRsdTunnel(serial=udid, autopair=True)` (uses `UserspaceRsdTunnel` via
     `CoreDeviceTunnelProxy` on iOS 17.4+, with rootless `NativeRemotedTunnel` fallback on macOS
     for iOS 17.0-17.3 and `RemotePairing` fallback when needed).
   - Wi-Fi: `UserspaceRsdTunnel(provider_factory=wifi_provider)` over paired `TcpLockdownClient`
     (`port=62078, keep_alive=True`) + `CoreDeviceTunnelProxy`, with automatic transient-error
     retries and fallback to `create_using_mobdev2` and `UserspaceRsdTunnel(..., remotepairing=True)`.
   - Legacy iOS < 17 fallback: direct `LockdownClient` (`DvtProvider(lockdown)`).
2. `UserspaceDialPlane` Routing Integrity:
   - Always passes the exact `self._rsd` instance returned by `await tunnel.aopen()` into
     `DvtProvider(self._rsd)`. Never opens a raw OS socket `RemoteServiceDiscoveryService((host, port))`.
3. Persistent DVT Keep-Alive for iOS 17-27+:
   - Holds `DvtProvider` and `LocationSimulation` entered on an `AsyncExitStack` on the persistent
     event loop for the entire simulation session (closing them immediately reverts GPS on iOS 17+).
   - Reuses the open `LocationSimulation` channel (~5 ms per coordinate update) for smooth routes
     and arrow-key movement, plus runs a periodic 15s keep-alive coordinate refresh task.
4. Transport Disconnect Watcher & Auto-Recovery:
   - Hooks `UserspaceRsdTunnel._transport_watcher` so unexpected Wi-Fi/USB drops are detected
     immediately; attempts one seamless Wi-Fi tunnel + GPS re-assertion before emitting `connection_reset`.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, suppress
from typing import Any, Callable, Optional

from geoport.config import LOCATION_KEEPALIVE_INTERVAL, LOCKDOWN_TCP_PORT, logger
from geoport.core.pair_records import (
    get_local_pair_record,
    has_remote_pair_record,
    install_headless_usbmux_patch,
    persist_pair_record_copy,
    update_device_meta_cache,
)

install_headless_usbmux_patch()

from pymobiledevice3.exceptions import InvalidServiceError
from pymobiledevice3.lockdown import (
    LockdownClient,
    create_using_mobdev2,
    create_using_tcp,
    create_using_usbmux,
)
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.rsd_tunnel import PreferredRsdTunnel
from pymobiledevice3.remote.tunnel_service import CoreDeviceTunnelProxy
from pymobiledevice3.remote.userspace_tunnel import TunnelProvider, UserspaceRsdTunnel, UserspaceTun
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

from geoport.core.developer_mode import check_developer_mode
from geoport.core.models import Coordinate, SessionSnapshot
from geoport.core.mounter import mount_developer_image

SocketEmitter = Callable[[str, dict[str, Any]], None]

_TRANSIENT_TUNNEL_MARKERS = (
    'stream read less than specified amount',
    'IncompleteReadError',
    'Error in path (parsing) -> magic',
    'Connection reset',
    'ConnectionTerminatedError',
    'Broken pipe',
    'EOFError',
)


def is_ios_17_or_greater(version_str: Optional[str]) -> bool:
    """Return True if `version_str` major version >= 17 (defaults to True when unknown)."""
    if not version_str:
        return True
    try:
        major = int(str(version_str).strip().split('.')[0])
        return major >= 17
    except (ValueError, IndexError):
        return True


class DeveloperModeRequiredError(RuntimeError):
    """Raised when the target iOS 17+ device has Developer Mode disabled."""


class DeviceSessionManager:
    """Manages the single active device tunnel and DVT location simulation lifecycle."""

    def __init__(
        self,
        socket_emitter: Optional[SocketEmitter] = None,
        keepalive_interval: float = LOCATION_KEEPALIVE_INTERVAL,
    ) -> None:
        self._emit_fn = socket_emitter
        self._keepalive_interval = max(2.0, float(keepalive_interval))
        self._lock = None
        self._tunnel = None
        self._rsd = None
        self._legacy_lockdown = None
        self._closing_intentionally = False
        self._dvt_stack = None
        self._dvt = None
        self._sim = None
        self._keepalive_task = None
        self._target_coordinate = None
        self._snapshot = SessionSnapshot()
        self._last_connect_params = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._emit_fn is not None:
            try:
                self._emit_fn(event, payload)
            except Exception as exc:
                logger.debug(f'Socket emit ({event}) failed: {exc}')

    @property
    def snapshot(self) -> SessionSnapshot:
        return self._snapshot

    @property
    def target_coordinate(self) -> Optional[Coordinate]:
        return self._target_coordinate

    def update_target_coordinate(self, lat: float, lng: float) -> Coordinate:
        coord = Coordinate(latitude=float(lat), longitude=float(lng))
        coord.validate()
        self._target_coordinate = coord
        if self._snapshot.simulating:
            self._snapshot.current_coordinate = coord
        return coord

    def is_connected_to(self, udid: Optional[str]) -> bool:
        return bool(
            udid
            and self._snapshot.connected
            and self._snapshot.udid == udid
            and (self._rsd is not None or self._legacy_lockdown is not None)
        )

    async def connect(
        self,
        udid: str,
        conn_type: str = 'Network',
        ios_version: Optional[str] = None,
        wifi_address: Optional[str] = None,
        wifi_port: Optional[int] = None,
        wifi_endpoints: Optional[list[dict[str, Any]]] = None,
    ) -> SessionSnapshot:
        """Establish a fresh rootless Userspace RSD Tunnel (or iOS < 17 Lockdown session)."""
        async with self._get_lock():
            return await self._connect_locked(
                udid=udid,
                conn_type=conn_type,
                ios_version=ios_version,
                wifi_address=wifi_address,
                wifi_port=wifi_port,
                wifi_endpoints=wifi_endpoints,
            )

    async def _connect_locked(
        self,
        udid: str,
        conn_type: str = 'Network',
        ios_version: Optional[str] = None,
        wifi_address: Optional[str] = None,
        wifi_port: Optional[int] = None,
        wifi_endpoints: Optional[list[dict[str, Any]]] = None,
    ) -> SessionSnapshot:
        if not udid:
            raise ValueError('Device UDID is required to connect')
        normalized_type = 'USB' if str(conn_type).upper() == 'USB' else 'Network'
        await self._disconnect_locked(clear_gps=False)
        self._last_connect_params = {
            'udid': udid,
            'conn_type': normalized_type,
            'ios_version': ios_version,
            'wifi_address': wifi_address,
            'wifi_port': wifi_port,
            'wifi_endpoints': list(wifi_endpoints or []),
        }
        if normalized_type == 'USB':
            return await self._connect_usb_locked(udid=udid, ios_version=ios_version)
        return await self._connect_wifi_locked(
            udid=udid,
            ios_version=ios_version,
            wifi_address=wifi_address,
            wifi_port=wifi_port,
            wifi_endpoints=wifi_endpoints,
        )

    async def _connect_usb_locked(self, udid: str, ios_version: Optional[str]) -> SessionSnapshot:
        logger.info(f'Connecting over USB to {udid} (reported iOS={ios_version})')
        ld = await create_using_usbmux(serial=udid, connection_type='USB', autopair=True)
        try:
            info = dict(ld.short_info or {})
            detected_ios = ld.product_version or info.get('ProductVersion') or ios_version
            device_name = info.get('DeviceName')
            if getattr(ld, 'pair_record', None):
                persist_pair_record_copy(udid, ld.pair_record)
            with suppress(Exception):
                if not await ld.get_enable_wifi_connections():
                    await ld.set_enable_wifi_connections(True)
            update_device_meta_cache(udid, info)
            if is_ios_17_or_greater(detected_ios):
                dev_mode = await ld.get_developer_mode_status()
                if not dev_mode:
                    raise DeveloperModeRequiredError('Developer Mode is disabled on this device.')
            else:
                self._legacy_lockdown = ld
                ld = None
                self._snapshot = SessionSnapshot(
                    connected=True,
                    udid=udid,
                    connection_type='USB',
                    transport='usb_lockdown_legacy',
                    ios_version=detected_ios,
                    device_name=device_name,
                    userspace_tunnel=False,
                )
                return self._snapshot
        finally:
            if ld is not None:
                with suppress(Exception):
                    await ld.close()
        tunnel = PreferredRsdTunnel(serial=udid, autopair=True)
        try:
            rsd = await tunnel.aopen()
        except BaseException as exc:
            with suppress(Exception):
                await tunnel.aclose()
            self._snapshot = SessionSnapshot(connected=False, udid=udid, last_error=str(exc))
            raise
        raw_handle = getattr(tunnel, '_handle', tunnel)
        usb_transport = (
            'usb_userspace_rsd' if isinstance(raw_handle, UserspaceRsdTunnel) else 'usb_native_remoted_rsd'
        )
        return await self._finalize_rsd_session_locked(
            tunnel=tunnel,
            rsd=rsd,
            udid=udid,
            connection_type='USB',
            transport=usb_transport,
            ios_version=detected_ios,
            device_name=device_name,
            wifi_address=None,
        )

    async def _connect_wifi_locked(
        self,
        udid: str,
        ios_version: Optional[str],
        wifi_address: Optional[str],
        wifi_port: Optional[int],
        wifi_endpoints: Optional[list[dict[str, Any]]],
    ) -> SessionSnapshot:
        candidate_hosts = []
        if wifi_address:
            candidate_hosts.append(wifi_address)
        for ep in wifi_endpoints or []:
            addr = ep.get('wifiAddress')
            if not addr:
                continue
            if addr not in candidate_hosts:
                candidate_hosts.append(addr)
        pair_record = get_local_pair_record(udid)
        has_rp = has_remote_pair_record(udid)
        if pair_record is None and not has_rp:
            raise RuntimeError(
                f"No Pair Record found for {udid}. Connect your iPhone once via USB (tap 'Trust This Computer') or place {udid}.plist in ~/.pymobiledevice3/."
            )
        detected_ios = ios_version
        device_name = None
        last_err = None
        if pair_record is not None:
            hosts_to_try = list(candidate_hosts) if candidate_hosts else [None]
            for host in hosts_to_try:
                for attempt in range(1, 6):
                    chosen_host_holder = {'host': host}
                    meta_holder = {'ios': detected_ios, 'name': device_name}

                    async def _wifi_provider_factory() -> TunnelProvider:
                        if chosen_host_holder['host']:
                            ld = await create_using_tcp(
                                hostname=chosen_host_holder['host'],
                                port=LOCKDOWN_TCP_PORT,
                                identifier=udid,
                                autopair=False,
                                pair_record=pair_record,
                                keep_alive=True,
                            )
                        else:
                            ld = await create_using_mobdev2(udid=udid, timeout=4.0)
                            chosen_host_holder['host'] = getattr(ld, 'hostname', None)
                        try:
                            info = dict(ld.short_info or {})
                            meta_holder['ios'] = (
                                ld.product_version or info.get('ProductVersion') or meta_holder['ios']
                            )
                            meta_holder['name'] = info.get('DeviceName') or meta_holder['name']
                            update_device_meta_cache(udid, info)
                            if is_ios_17_or_greater(meta_holder['ios']):
                                dev_status = await ld.get_developer_mode_status()
                                if not dev_status:
                                    raise DeveloperModeRequiredError(
                                        'Developer Mode is disabled on this device.'
                                    )
                            svc = await CoreDeviceTunnelProxy.create(ld)
                            return TunnelProvider(service=svc, lockdown=ld)
                        except BaseException:
                            with suppress(Exception):
                                await ld.close()
                            raise

                    if detected_ios and (not is_ios_17_or_greater(detected_ios)) and host:
                        ld_legacy = await create_using_tcp(
                            hostname=host,
                            port=LOCKDOWN_TCP_PORT,
                            identifier=udid,
                            autopair=False,
                            pair_record=pair_record,
                            keep_alive=True,
                        )
                        self._legacy_lockdown = ld_legacy
                        info = dict(ld_legacy.short_info or {})
                        self._snapshot = SessionSnapshot(
                            connected=True,
                            udid=udid,
                            connection_type='Network',
                            transport='wifi_lockdown_legacy',
                            ios_version=ld_legacy.product_version or detected_ios,
                            device_name=info.get('DeviceName'),
                            wifi_address=host,
                            userspace_tunnel=False,
                        )
                        return self._snapshot
                    tunnel = UserspaceRsdTunnel(provider_factory=_wifi_provider_factory)
                    try:
                        logger.info(
                            f'Establishing Wi-Fi Userspace RSD Tunnel to {host or "mobdev2"} for {udid} (attempt {attempt}/5)'
                        )
                        rsd = await tunnel.aopen()
                        return await self._finalize_rsd_session_locked(
                            tunnel=tunnel,
                            rsd=rsd,
                            udid=udid,
                            connection_type='Network',
                            transport='wifi_lockdown_userspace_rsd',
                            ios_version=meta_holder['ios'] or rsd.product_version or detected_ios,
                            device_name=meta_holder['name'],
                            wifi_address=chosen_host_holder['host'] or wifi_address,
                        )
                    except DeveloperModeRequiredError:
                        with suppress(Exception):
                            await tunnel.aclose()
                        raise
                    except InvalidServiceError as exc:
                        with suppress(Exception):
                            await tunnel.aclose()
                        last_err = exc
                        logger.warning(
                            f'CoreDeviceTunnelProxy unavailable over Wi-Fi Lockdown ({exc}); trying RemotePairing'
                        )
                        break
                    except Exception as exc:
                        with suppress(Exception):
                            await tunnel.aclose()
                        last_err = exc
                        msg = f'{type(exc).__name__}: {exc}'
                        is_transient = any(marker in msg for marker in _TRANSIENT_TUNNEL_MARKERS)
                        if is_transient and attempt < 5:
                            logger.warning(
                                f'Transient Wi-Fi tunnel handshake error (attempt {attempt}/5): {msg}'
                            )
                            await asyncio.sleep(0.8)
                            continue
                        logger.warning(f'Wi-Fi Lockdown tunnel attempt failed for host={host}: {msg}')
                        break
        if has_rp:
            tunnel_rp = UserspaceRsdTunnel(serial=udid, autopair=False, remotepairing=True)
            try:
                logger.info(f'Attempting Wi-Fi RemotePairing Userspace RSD Tunnel for {udid}')
                rsd = await tunnel_rp.aopen()
                dev_status = await check_developer_mode(udid=udid, rsd=rsd)
                if dev_status is False:
                    await tunnel_rp.aclose()
                    raise DeveloperModeRequiredError('Developer Mode is disabled on this device.')
                return await self._finalize_rsd_session_locked(
                    tunnel=tunnel_rp,
                    rsd=rsd,
                    udid=udid,
                    connection_type='Network',
                    transport='wifi_remotepairing_userspace_rsd',
                    ios_version=rsd.product_version or detected_ios,
                    device_name=device_name,
                    wifi_address=wifi_address,
                )
            except DeveloperModeRequiredError:
                raise
            except Exception as rp_exc:
                with suppress(Exception):
                    await tunnel_rp.aclose()
                last_err = rp_exc
                logger.error(f'Wi-Fi RemotePairing Userspace RSD Tunnel failed for {udid}: {rp_exc}')
        err_msg = str(last_err) if last_err else 'Unable to establish Wi-Fi Userspace RSD Tunnel'
        self._snapshot = SessionSnapshot(connected=False, udid=udid, last_error=err_msg)
        raise RuntimeError(err_msg) from last_err

    async def _finalize_rsd_session_locked(
        self,
        tunnel: UserspaceRsdTunnel | PreferredRsdTunnel,
        rsd: RemoteServiceDiscoveryService,
        udid: str,
        connection_type: str,
        transport: str,
        ios_version: Optional[str],
        device_name: Optional[str],
        wifi_address: Optional[str],
    ) -> SessionSnapshot:
        self._tunnel = tunnel
        self._rsd = rsd
        self._closing_intentionally = False
        rsd_addr = None
        rsd_port = None
        svc_addr = getattr(getattr(rsd, 'service', None), 'address', None)
        if isinstance(svc_addr, (tuple, list)) and len(svc_addr) >= 2:
            rsd_addr = str(svc_addr[0])
            rsd_port = int(svc_addr[1])
        raw_handle = getattr(tunnel, '_handle', tunnel)
        tun_obj = getattr(raw_handle, 'tun', None)
        is_userspace = isinstance(tun_obj, UserspaceTun) or isinstance(raw_handle, UserspaceRsdTunnel)
        resolved_ios = ios_version or getattr(rsd, 'product_version', None)
        peer_info = getattr(rsd, 'peer_info', None) or {}
        resolved_name = device_name or peer_info.get(' DeviceName') or peer_info.get('DeviceName')
        update_device_meta_cache(
            udid,
            {
                'DeviceName': resolved_name,
                'ProductVersion': resolved_ios,
                'ProductType': peer_info.get('ProductType'),
                'BuildVersion': peer_info.get('BuildVersion'),
            },
        )
        self._snapshot = SessionSnapshot(
            connected=True,
            udid=udid,
            connection_type=connection_type,
            transport=transport,
            ios_version=resolved_ios,
            device_name=resolved_name,
            rsd_address=rsd_addr,
            rsd_port=rsd_port,
            wifi_address=wifi_address,
            userspace_tunnel=is_userspace,
            simulating=False,
            current_coordinate=None,
            last_error=None,
        )
        watcher_task = getattr(raw_handle, '_transport_watcher', None)
        if isinstance(watcher_task, asyncio.Task):
            watcher_task.add_done_callback(
                lambda task, expected_tunnel=tunnel: asyncio.get_running_loop().create_task(
                    self._on_unexpected_transport_closed(expected_tunnel, task)
                )
            )
        logger.info(
            f'Userspace RSD Tunnel active: udid={udid} transport={transport} endpoint=[{rsd_addr}]:{rsd_port} userspace={is_userspace}'
        )
        return self._snapshot

    async def _on_unexpected_transport_closed(
        self,
        expected_tunnel: UserspaceRsdTunnel | PreferredRsdTunnel,
        watcher_task: asyncio.Task[Any],
    ) -> None:
        if watcher_task.cancelled() or self._closing_intentionally:
            return
        async with self._get_lock():
            if self._tunnel is not expected_tunnel or self._closing_intentionally:
                return
            was_simulating = self._snapshot.simulating
            saved_coord = self._snapshot.current_coordinate or self._target_coordinate
            saved_params = dict(self._last_connect_params) if self._last_connect_params else None
            logger.warning('Userspace RSD tunnel transport dropped unexpectedly; cleaning up DVT state.')
            await self._close_dvt_locked(send_clear=False)
            self._tunnel = None
            self._rsd = None
            self._snapshot.connected = False
            self._snapshot.simulating = False
            if (
                was_simulating
                and saved_coord
                and saved_params
                and saved_params.get('conn_type') == 'Network'
            ):
                try:
                    logger.info(
                        'Attempting automatic Wi-Fi Userspace RSD Tunnel recovery & GPS re-assertion...'
                    )
                    await asyncio.sleep(1.0)
                    await self._connect_locked(**saved_params)
                    await self._set_location_locked(saved_coord.latitude, saved_coord.longitude)
                    self._emit(
                        'tunnel_reconnected',
                        {
                            'message': 'Wi-Fi tunnel reconnected automatically; simulated location restored.',
                            'status': self._snapshot.to_dict(),
                        },
                    )
                except Exception as exc:
                    logger.error(f'Automatic Wi-Fi tunnel recovery failed: {exc}')
                else:
                    return
            msg = 'Device tunnel connection closed unexpectedly.'
            self._snapshot.last_error = msg
            self._emit('connection_reset', {'message': msg})

    async def _ensure_sim_locked(self) -> LocationSimulation:
        """Return an open `LocationSimulation` service bound to the active tunnel's RSD."""
        if self._sim is not None:
            return self._sim
        if self._rsd is not None:
            provider = self._rsd
        elif self._legacy_lockdown is not None:
            await mount_developer_image(
                udid=self._snapshot.udid,
                lockdown_client=self._legacy_lockdown,
            )
            provider = self._legacy_lockdown
        else:
            raise RuntimeError('No active device tunnel. Please connect to a device first.')
        stack = AsyncExitStack()
        try:
            dvt = await stack.enter_async_context(DvtProvider(provider))
            sim = await stack.enter_async_context(LocationSimulation(dvt))
        except BaseException:
            await stack.aclose()
            raise
        self._dvt_stack = stack
        self._dvt = dvt
        self._sim = sim
        return sim

    async def set_location(
        self,
        lat: Optional[float] = None,
        lng: Optional[float] = None,
    ) -> SessionSnapshot:
        """Apply and hold simulated GPS coordinates over the persistent DVT channel."""
        async with self._get_lock():
            if lat is not None and lng is not None:
                coord = self.update_target_coordinate(lat, lng)
            elif self._target_coordinate is not None:
                coord = self._target_coordinate
            else:
                raise ValueError('No location coordinate specified')
            return await self._set_location_locked(coord.latitude, coord.longitude)

    async def _set_location_locked(self, lat: float, lng: float) -> SessionSnapshot:
        coord = Coordinate(latitude=float(lat), longitude=float(lng))
        coord.validate()
        self._target_coordinate = coord
        try:
            sim = await self._ensure_sim_locked()
            await sim.set(coord.latitude, coord.longitude)
        except Exception as first_exc:
            logger.warning(
                f'DVT LocationSimulation.set failed ({first_exc}); reopening DVT channel once...'
            )
            await self._close_dvt_locked(send_clear=False)
            sim = await self._ensure_sim_locked()
            await sim.set(coord.latitude, coord.longitude)
        self._snapshot.simulating = True
        self._snapshot.current_coordinate = coord
        self._snapshot.last_error = None
        self._ensure_keepalive_task_locked()
        logger.info(
            f'Simulated GPS set to ({coord.latitude:.6f}, {coord.longitude:.6f}) on {self._snapshot.udid}'
        )
        return self._snapshot

    def _ensure_keepalive_task_locked(self) -> None:
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return
        self._keepalive_task = asyncio.create_task(
            self._location_keepalive_loop(),
            name='geoport-dvt-location-keepalive',
        )

    async def _location_keepalive_loop(self) -> None:
        """Periodically re-assert the simulated coordinate while `DvtProvider` stays open."""
        try:
            while True:
                await asyncio.sleep(self._keepalive_interval)
                async with self._get_lock():
                    if (
                        not self._snapshot.simulating
                        or self._sim is None
                        or self._snapshot.current_coordinate is None
                    ):
                        return
                    coord = self._snapshot.current_coordinate
                    try:
                        await self._sim.set(coord.latitude, coord.longitude)
                    except Exception as exc:
                        logger.warning(f'Location keep-alive refresh error: {exc}')
                        return
        except asyncio.CancelledError:
            return

    async def clear_location(self) -> SessionSnapshot:
        """Clear simulated GPS location on the device while keeping the RSD tunnel connected."""
        async with self._get_lock():
            await self._close_dvt_locked(send_clear=True)
            self._snapshot.simulating = False
            self._snapshot.current_coordinate = None
            logger.info(f'Simulated GPS cleared on {self._snapshot.udid}')
            return self._snapshot

    async def _close_dvt_locked(self, send_clear: bool = False) -> None:
        if self._keepalive_task is not None:
            task, self._keepalive_task = self._keepalive_task, None
            if task is not asyncio.current_task():
                task.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await task
        if send_clear and (self._rsd is not None or self._legacy_lockdown is not None):
            try:
                sim = await self._ensure_sim_locked()
                await sim.clear()
            except Exception as exc:
                logger.warning(f'LocationSimulation.clear() encountered non-fatal error: {exc}')
        stack, self._dvt_stack = self._dvt_stack, None
        self._sim = None
        self._dvt = None
        if stack is not None:
            with suppress(Exception):
                await stack.aclose()

    async def disconnect(self, clear_gps: bool = True) -> SessionSnapshot:
        """Stop location simulation (optional) and tear down the active Userspace RSD Tunnel."""
        async with self._get_lock():
            await self._disconnect_locked(clear_gps=clear_gps)
            return self._snapshot

    async def _disconnect_locked(self, clear_gps: bool = False) -> None:
        self._closing_intentionally = True
        try:
            await self._close_dvt_locked(send_clear=clear_gps and self._snapshot.simulating)
            if self._tunnel is not None:
                tunnel, self._tunnel = self._tunnel, None
                self._rsd = None
                with suppress(Exception):
                    await tunnel.aclose()
            if self._legacy_lockdown is not None:
                ld, self._legacy_lockdown = self._legacy_lockdown, None
                with suppress(Exception):
                    await ld.close()
            self._snapshot = SessionSnapshot(connected=False)
        finally:
            self._closing_intentionally = False
